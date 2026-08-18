"""Scheduled maintenance tasks (Step 4).

`daily_expiry_scan` flags Live BGs approaching expiry and marks overdue
extension requests. `daily_extension_digest` sends one digest email per
Coordinator summarizing open extension items. Both are registered on Celery
Beat. A single BG's processing error never aborts the scan for the others.
"""
from datetime import date, datetime, timedelta

from bgcc.celery import celery
from bgcc.extensions import db
from bgcc.models.enums import BGStatus
from bgcc.models.lifecycle import BgInvocation, ExtensionRequest
from bgcc.models.reference import BankGuarantee
from bgcc.models.settings import ApplicationSetting
from bgcc.models.users import User


def _policy():
    setting = ApplicationSetting.query.filter_by(setting_key="extension_policy").first()
    value = (setting.setting_value if setting else {}) or {}
    return {
        "warning_days": int(value.get("warning_days", 45)),
        "overdue_days": int(value.get("overdue_days", 21)),
    }


@celery.task(bind=True, name="maintenance.daily_expiry_scan", max_retries=1)
def daily_expiry_scan(self):
    policy = _policy()
    today = date.today()
    live_bgs = BankGuarantee.query.filter_by(status=BGStatus.live.value).all()
    created = updated = overdue = 0

    for bg in live_bgs:
        try:
            days = (bg.expiry_date - today).days
            req = (
                ExtensionRequest.query.filter_by(parent_bg_id=bg.id)
                .order_by(ExtensionRequest.id.desc())
                .first()
            )
            if req is None:
                if days <= policy["warning_days"]:
                    db.session.add(ExtensionRequest(parent_bg_id=bg.id, stage="not_started"))
                    created += 1
                    req = (
                        ExtensionRequest.query.filter_by(parent_bg_id=bg.id)
                        .order_by(ExtensionRequest.id.desc())
                        .first()
                    )
                else:
                    continue
            if days <= policy["overdue_days"] and req.stage in ("not_started", "requested") and not req.is_overdue:
                req.is_overdue = True
                overdue += 1
            elif days > policy["overdue_days"] and req.stage not in ("not_started", "requested"):
                updated += 0
        except Exception as exc:
            self.logger.exception("expiry scan failed for bg=%s: %s", bg.id, exc)
            db.session.rollback()
            continue
    db.session.commit()
    return {"created": created, "overdue": overdue}


@celery.task(bind=True, name="maintenance.daily_extension_digest", max_retries=2)
def daily_extension_digest(self):
    today = date.today()
    open_reqs = ExtensionRequest.query.filter(
        ExtensionRequest.stage.in_(["not_started", "requested"])
    ).all()

    by_coordinator = {}
    for req in open_reqs:
        bg = db.session.get(BankGuarantee, req.parent_bg_id)
        if not bg:
            continue
        days = (bg.expiry_date - today).days
        item = {
            "bg_number": bg.bg_number,
            "vendor_name": bg.vendor_name or "n/a",
            "expiry_date": str(bg.expiry_date),
            "days": days,
            "overdue": req.is_overdue or days < 0,
        }
        coord_id = req.coordinator_id
        if coord_id is None:
            holders = User.query.filter(
                User.is_approved.is_(True), User.is_active.is_(True),
                User.sap_system_id == bg.sap_system_id,
            ).all()
            coords = [h for h in holders if "coordinator" in (h.granted_roles or [])]
            coord_id = coords[0].id if coords else None
        if coord_id is None:
            continue
        by_coordinator.setdefault(coord_id, []).append(item)

    for coord_id, items in by_coordinator.items():
        try:
            user = db.session.get(User, coord_id)
            if not user:
                continue
            _send_digest(user, items)
        except Exception as exc:
            self.logger.exception("digest failed for coordinator %s: %s", coord_id, exc)
    return {"coordinators": len(by_coordinator)}


@celery.task(bind=True, name="maintenance.daily_claim_window_scan", max_retries=1)
def daily_claim_window_scan(self):
    """Flag Live BGs approaching/critical in their invocation claim window.

    Creates `bg_invocations` rows at the approaching threshold and advances them
    to `critical` at the critical threshold. Purely informational - it never
    triggers an invocation itself; it ensures BU FC has advance warning.
    """
    from bgcc.services.invocation_service import invocation_policy, claim_window_date

    policy = invocation_policy()
    today = date.today()
    live_bgs = BankGuarantee.query.filter_by(status=BGStatus.live.value).all()
    created = advanced = 0

    for bg in live_bgs:
        try:
            days = (claim_window_date(bg) - today).days
            inv = BgInvocation.query.filter_by(bank_guarantee_id=bg.id).first()
            if inv is None:
                if days <= policy["approaching_days"]:
                    stage = "critical" if days <= policy["critical_days"] else "approaching_window"
                    db.session.add(BgInvocation(bank_guarantee_id=bg.id, stage=stage))
                    created += 1
                continue
            if inv.stage == "approaching_window" and days <= policy["critical_days"]:
                inv.stage = "critical"
                advanced += 1
        except Exception as exc:
            self.logger.exception("claim-window scan failed for bg=%s: %s", bg.id, exc)
            db.session.rollback()
            continue
    db.session.commit()
    return {"created": created, "advanced": advanced}


@celery.task(bind=True, name="maintenance.warm_dashboard_cache", max_retries=2)
def warm_dashboard_cache(self):
    from bgcc.services.analytics_service import warm_dashboard_cache as warm

    try:
        warm()
        return {"ok": True}
    except Exception as exc:
        self.logger.exception("dashboard cache warm failed: %s", exc)
        raise


@celery.task(bind=True, name="maintenance.bank_verification_poll", max_retries=2)
def bank_verification_poll(self):
    from bgcc.services.bank_verification_service import poll_pending

    try:
        result = poll_pending(self)
        return result
    except Exception as exc:
        self.logger.exception("bank verification poll failed: %s", exc)
        raise


def _send_digest(user, items):
    from bgcc.services.notification_service import dispatch
    from bgcc.utils.urls import build_absolute_url

    overdue_items = [i for i in items if i["overdue"]]
    approaching = [i for i in items if not i["overdue"]]
    lines = []
    if overdue_items:
        lines.append("OVERDUE:")
        for i in overdue_items:
            lines.append(f"  • {i['bg_number']} ({i['vendor_name']}) - expired {i['days']} days ago / overdue")
    if approaching:
        lines.append("Approaching expiry:")
        for i in approaching:
            lines.append(f"  • {i['bg_number']} ({i['vendor_name']}) - {i['days']} days remaining")
    if not lines:
        return
    ext_url = build_absolute_url("/lifecycle/extensions")
    body = "Your extension digest for today:\n\n" + "\n".join(lines) + \
        f"\n\nPlease follow up on these extensions in BG Command Centre:\n\nDirect link: {ext_url}"
    dispatch(
        user_id=user.id,
        notification_type="extension_digest",
        title=f"Extension digest - {len(items)} open item(s)",
        body="\n".join(lines),
        link_url=ext_url,
        email_to=user.email,
        email_subject=f"BG Command Centre extension digest ({len(items)} item(s))",
        email_body=body,
        template_name="emails/extension_digest.html",
        template_context={"items": items, "action_url": ext_url, "link_url": ext_url},
    )
