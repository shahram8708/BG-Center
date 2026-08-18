"""BG invocation & legal letter core logic (Step 5).

Includes the claim-window monitoring helpers, draft-generation orchestration,
the race-safe dual-gate send evaluator, the hold workflow, and the registration
of this step's magic-link targets against `bg_invocations`' existing columns -
all reusing Step 4's generic magic-link service unchanged.
"""
from datetime import date, datetime

from bgcc.extensions import db
from bgcc.models.deviations import Deviation
from bgcc.models.enums import BGStatus, WorkflowAction
from bgcc.models.generated_documents import GeneratedDocument
from bgcc.models.lifecycle import BgInvocation
from bgcc.models.reference import BankGuarantee
from bgcc.models.settings import ApplicationSetting
from bgcc.models.workflow import WorkflowHistory
from bgcc.services import audit_service, magic_link_service

import logging
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

INVOCATION_DOC_KIND = "invocation_letter"


def get_current_app_date():
    try:
        from flask import current_app
        tz_name = current_app.config.get("TIMEZONE", "Asia/Kolkata") if current_app else "Asia/Kolkata"
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return date.today()


def invocation_policy():
    setting = ApplicationSetting.query.filter_by(setting_key="invocation_policy").first()
    value = (setting.setting_value if setting else {}) or {}
    return {
        "approaching_days": int(value.get("approaching_days", 15)),
        "critical_days": int(value.get("critical_days", 5)),
    }


def claim_window_date(bg):
    return bg.claim_expiry_date or bg.expiry_date


def claim_window_days(bg, current_d=None):
    today = current_d or get_current_app_date()
    c_date = claim_window_date(bg)
    return (c_date - today).days if c_date else 0


def evaluate_claim_window(bg, current_d=None):
    """Calculate whether the current date falls within the configured claim window."""
    policy = invocation_policy()
    today = current_d or get_current_app_date()
    claim_date = claim_window_date(bg)
    if not claim_date:
        days_remaining = 0
        is_in_window = False
        is_critical = False
        is_approaching = False
        is_outside_window = True
        is_expired = False
        days_until_window = 0
    else:
        days_remaining = (claim_date - today).days
        is_expired = days_remaining < 0
        is_critical = (0 <= days_remaining <= policy["critical_days"])
        is_approaching = (policy["critical_days"] < days_remaining <= policy["approaching_days"])
        is_in_window = (0 <= days_remaining <= policy["approaching_days"])
        is_outside_window = (days_remaining > policy["approaching_days"])
        days_until_window = max(0, days_remaining - policy["approaching_days"])

    result = {
        "claim_date": claim_date,
        "days_remaining": days_remaining,
        "is_in_window": is_in_window,
        "is_critical": is_critical,
        "is_approaching": is_approaching,
        "is_outside_window": is_outside_window,
        "is_expired": is_expired,
        "days_until_window": days_until_window,
        "can_generate_draft": is_in_window,
        "approaching_days": policy["approaching_days"],
        "critical_days": policy["critical_days"],
    }
    logger.debug(
        "BG %s (id=%s) claim window evaluation: claim_date=%s, today=%s, days_remaining=%s, "
        "is_in_window=%s, is_critical=%s, can_generate_draft=%s",
        bg.bg_number, bg.id, claim_date, today, days_remaining, is_in_window, is_critical, is_in_window
    )
    return result


def get_live_bgs_for_user(user):
    """Fetch all Live / Approved Bank Guarantees accessible to the user."""
    query = BankGuarantee.query.filter(
        db.or_(
            BankGuarantee.status == BGStatus.live,
            BankGuarantee.status == BGStatus.live.value,
            BankGuarantee.status == "live",
            BankGuarantee.status == "approved",
            BankGuarantee.status == "Live",
            BankGuarantee.status == "Approved",
            BankGuarantee.current_stage == "live",
            BankGuarantee.current_stage == "approved",
            BankGuarantee.current_stage == "Live",
            BankGuarantee.current_stage == "Approved",
        )
    )
    user_roles = set(user.granted_roles or []) if user else set()
    is_admin = "admin" in user_roles or getattr(user, "active_role", None) == "admin"

    if not is_admin and getattr(user, "sap_system_id", None):
        query = query.filter(BankGuarantee.sap_system_id == user.sap_system_id)

    bgs = query.order_by(BankGuarantee.expiry_date.asc()).all()
    logger.info(
        "Fetched %s Live Bank Guarantees for user %s (role=%s, sap_system_id=%s, is_admin=%s)",
        len(bgs), getattr(user, "email", None), getattr(user, "active_role", None),
        getattr(user, "sap_system_id", None), is_admin
    )
    return bgs


def get_or_create_invocation(bg):
    inv = BgInvocation.query.filter_by(bank_guarantee_id=bg.id).first()
    if inv is None:
        inv = BgInvocation(bank_guarantee_id=bg.id, stage="approaching_window")
        db.session.add(inv)
        db.session.flush()
    return inv


def latest_invocation(bg):
    return BgInvocation.query.filter_by(bank_guarantee_id=bg.id).first()


def highest_deviation_tier(bg):
    tiers = [d.effective_tier for d in Deviation.query.filter_by(bank_guarantee_id=bg.id).all()
             if d.effective_tier]
    if "prohibited" in tiers:
        return "prohibited"
    if "high" in tiers:
        return "high"
    if "low" in tiers:
        return "low"
    return None


# --------------------------------------------------------------- magic links

def _cfo_email():
    setting = ApplicationSetting.query.filter_by(setting_key="executive_contacts").first()
    value = (setting.setting_value if setting else {}) or {}
    return value.get("cfo_email") or value.get("cfo")


def _ceo_email():
    setting = ApplicationSetting.query.filter_by(setting_key="executive_contacts").first()
    value = (setting.setting_value if setting else {}) or {}
    return value.get("ceo_email") or value.get("ceo")


def _bg(inv):
    return db.session.get(BankGuarantee, inv.bank_guarantee_id)


def _on_ceo_approve(record):
    # Main CEO approval of the invocation. Timestamp already written by the
    # magic-link service; now attempt the send if both gates are clear.
    evaluate_and_send(record)


def _on_ceo_decline(record):
    bg = _bg(record)
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage=record.stage, to_stage="ceo_declined",
        action=WorkflowAction.executive_declined, actor_role="ceo",
        comments="CEO declined the invocation; requires human follow-up.",
    ))
    db.session.commit()
    audit_service.record(
        "invocation_ceo_declined", target_type="bg_invocation", target_id=record.id,
        metadata_json={"bg_number": bg.bg_number},
    )
    _notify_declined(bg, record.id)


def _notify_declined(bg, invocation_id):
    from bgcc.models.users import User
    from bgcc.services.notification_service import dispatch
    from bgcc.utils.urls import build_absolute_url

    detail_url = build_absolute_url(f"/bg/{bg.id}")
    holders = User.query.filter(
        User.is_approved.is_(True), User.is_active.is_(True),
        User.sap_system_id == bg.sap_system_id,
    ).all()
    for holder in holders:
        if set(holder.granted_roles or []) & {"bu_fc", "tc_head"}:
            dispatch(
                user_id=holder.id, notification_type="invocation_declined",
                title="CEO declined an invocation",
                body=f"The CEO declined the invocation for {bg.bg_number}. Human follow-up is required.",
                link_url=detail_url, email_to=holder.email,
                email_subject="CEO declined an invocation",
                email_body=f"The CEO declined the invocation for {bg.bg_number}.\n\nDirect link: {detail_url}",
                template_name="emails/notification.html",
                template_context={
                    "details": {
                        "Guarantee Number": bg.bg_number,
                        "Vendor": bg.vendor_name or "N/A",
                        "Status": "Declined by CEO",
                    },
                    "action_text": "View Guarantee Details",
                    "action_url": detail_url,
                    "link_url": detail_url,
                },
                triggered_by=None,
            )


# ---- Hold approval targets (sequential CFO-then-CEO) ----

def _on_hold_cfo_approve(record):
    # CFO approved the hold; sequentially dispatch the CEO hold approval.
    email = _ceo_email()
    if not email:
        raise ValueError("No CEO contact email is configured for the hold approval.")
    bg = _bg(record)
    magic_link_service.issue_and_email(
        "invocation_hold_ceo", record, email,
        extra_lines=[
            f"Bank Guarantee: {bg.bg_number}",
            "An invocation hold requires your CEO approval.",
        ],
    )


def _on_hold_cfo_decline(record):
    _release_hold(record, "CFO declined the invocation hold.")


def _on_hold_ceo_approve(record):
    # CEO approved the hold; it remains in effect. Enforce sequential gate.
    if record.hold_cfo_approved_at is None:
        raise ValueError("CFO hold approval must be recorded before CEO hold approval.")


def _on_hold_ceo_decline(record):
    _release_hold(record, "CEO declined the invocation hold.")


def _register_targets():
    if _register_targets.done:
        return
    magic_link_service.register_target(
        key="invocation_ceo",
        model=BgInvocation,
        token_attr="ceo_approval_token",
        ts_attr="ceo_approved_at",
        salt="invocation-ceo-approval",
        max_age_settings_key="executive_approval_expiry_hours",
        max_age_default=72 * 3600,
        title="CEO approval required - Bank Guarantee invocation",
        subject="A Bank Guarantee invocation requires your CEO approval",
        on_approve=_on_ceo_approve,
        on_decline=_on_ceo_decline,
    )
    magic_link_service.register_target(
        key="invocation_hold_cfo",
        model=BgInvocation,
        token_attr="hold_cfo_approval_token",  # resolved below
        ts_attr="hold_cfo_approved_at",
        salt="invocation-hold-cfo",
        max_age_settings_key="executive_approval_expiry_hours",
        max_age_default=72 * 3600,
        title="CFO approval required - Bank Guarantee invocation hold",
        subject="A Bank Guarantee invocation hold requires your CFO approval",
        on_approve=_on_hold_cfo_approve,
        on_decline=_on_hold_cfo_decline,
    )
    magic_link_service.register_target(
        key="invocation_hold_ceo",
        model=BgInvocation,
        token_attr="hold_ceo_approval_token",  # resolved below
        ts_attr="hold_ceo_approved_at",
        salt="invocation-hold-ceo",
        max_age_settings_key="executive_approval_expiry_hours",
        max_age_default=72 * 3600,
        title="CEO approval required - Bank Guarantee invocation hold",
        subject="A Bank Guarantee invocation hold requires your CEO approval",
        on_approve=_on_hold_ceo_approve,
        on_decline=_on_hold_ceo_decline,
    )
    _register_targets.done = True


_register_targets.done = False


def dispatch_ceo_approval(inv):
    _register_targets()
    email = _ceo_email()
    if not email:
        raise ValueError("No CEO contact email is configured.")
    bg = _bg(inv)
    magic_link_service.issue_and_email(
        "invocation_ceo", inv, email,
        extra_lines=[
            f"Bank Guarantee: {bg.bg_number}",
            f"Vendor: {bg.vendor_name or 'n/a'}",
            "An invocation claim requires your CEO approval before it can be sent to the bank.",
        ],
    )


def dispatch_hold_cfo_approval(inv):
    _register_targets()
    email = _cfo_email()
    if not email:
        raise ValueError("No CFO contact email is configured for the hold approval.")
    bg = _bg(inv)
    magic_link_service.issue_and_email(
        "invocation_hold_cfo", inv, email,
        extra_lines=[
            f"Bank Guarantee: {bg.bg_number}",
            "An invocation hold requires your CFO approval.",
        ],
    )


def dispatch_hold_ceo_approval(inv):
    _register_targets()
    email = _ceo_email()
    if not email:
        raise ValueError("No CEO contact email is configured for the hold approval.")
    bg = _bg(inv)
    magic_link_service.issue_and_email(
        "invocation_hold_ceo", inv, email,
        extra_lines=[
            f"Bank Guarantee: {bg.bg_number}",
            "An invocation hold requires your CEO approval.",
        ],
    )


# ------------------------------------------------------------------ draft gen

def build_letter_content(bg, gemini_content):
    """Assemble the full letter content, with the amount-in-words computed
    deterministically (never by the AI)."""
    from bgcc.utils.numbers import amount_in_words

    amount = str(bg.amount) if bg.amount is not None else "0"
    currency = bg.currency or "INR"
    guarantee_labels = {
        "pbg": "Performance Bank Guarantee",
        "abg": "Advance Bank Guarantee",
        "cpbg": "CPBG",
        "cpbg_cum_pbg": "CPBG cum Performance Bank Guarantee",
        "cg": "Corporate Guarantee",
    }
    sender_name = "BG Command Centre"
    return {
        "sender_name": sender_name,
        "sender_address": "Corporate Office, Mumbai, India",
        "date": date.today().strftime("%d %B %Y"),
        "recipient_bank": gemini_content.get("recipient_bank", bg.issuing_bank or ""),
        "recipient_branch": gemini_content.get("recipient_branch", ""),
        "recipient_address": gemini_content.get("recipient_address", ""),
        "bg_number": bg.bg_number,
        "vendor_name": bg.vendor_name or "",
        "guarantee_type_label": guarantee_labels.get(bg.bg_type, bg.bg_type.upper()),
        "claim_amount_figures": f"{amount} {currency}",
        "claim_amount_words": amount_in_words(amount, currency),
        "claim_deadline": gemini_content.get("claim_deadline", ""),
        "invocation_phrasing": gemini_content.get("invocation_phrasing", ""),
        "signing_authority": gemini_content.get("signing_authority", ""),
    }


# ------------------------------------------------------------- send evaluator

def _internal_users(bg):
    from bgcc.models.users import User

    return User.query.filter(
        User.is_approved.is_(True), User.is_active.is_(True),
        User.sap_system_id == bg.sap_system_id,
    ).all()


def _bank_contact_email(bg):
    setting = ApplicationSetting.query.filter_by(setting_key="approved_banks").first()
    value = (setting.setting_value if setting else {}) or {}
    banks = value.get("banks", []) if isinstance(value, dict) else value or []
    for bank in banks:
        name = bank.get("name", "")
        if name and bg.issuing_bank and (
            name.lower() in bg.issuing_bank.lower()
            or bg.issuing_bank.lower() in name.lower()
        ):
            return bank.get("contact_email")
    return None


def perform_send(inv):
    """Atomic, idempotent send: mark sent + update BG status + history, then
    enqueue the actual notification/send tasks. Guards against double-send via
    the `sent_to_bank_at is None` check re-verified inside this transaction."""
    bg = _bg(inv)
    if inv.sent_to_bank_at is not None:
        return False
    # Re-verify all three gates inside the transaction.
    if not inv.signed_document_id or not inv.ceo_approved_at or inv.stage == "on_hold":
        return False

    inv.sent_to_bank_at = datetime.utcnow()
    inv.stage = "sent_to_bank"
    bg.status = BGStatus.submitted_to_bank
    bg.current_stage = BGStatus.submitted_to_bank.value
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage=inv.stage or "signed_uploaded",
        to_stage="sent_to_bank", action=WorkflowAction.invocation_sent_to_bank,
        actor_role="system", comments="Invocation cleared both gates and was sent to the bank.",
    ))
    db.session.commit()
    audit_service.record(
        "invocation_sent_to_bank", target_type="bg_invocation", target_id=inv.id,
        metadata_json={"bg_number": bg.bg_number},
    )

    from bgcc.tasks.invocation_tasks import notify_and_dispatch_send

    notify_and_dispatch_send.delay(inv.id)
    return True


def evaluate_and_send(inv):
    """Dual-gate evaluator. Safe to call multiple times; idempotent.

    Checks signed_document_id, ceo_approved_at, and not-on-hold, and that a
    send has not already happened. If a hold is in effect, does nothing.
    """
    if inv.stage == "on_hold":
        return False
    if inv.sent_to_bank_at is not None:
        return False
    if not inv.signed_document_id or not inv.ceo_approved_at:
        return False
    return perform_send(inv)


# ---------------------------------------------------------------------- hold

def request_hold(inv, user):
    """Immediately suspend the send path before any executive confirmation."""
    bg = _bg(inv)
    inv.stage = "on_hold"
    inv.hold_requested_by = user.id
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage=inv.stage, to_stage="on_hold",
        action=WorkflowAction.invocation_hold_requested, actor_id=user.id,
        actor_role=user.active_role,
        comments="TC Head requested a hold on the invocation.",
    ))
    db.session.commit()
    audit_service.record(
        "invocation_hold_requested", actor_id=user.id, target_type="bg_invocation",
        target_id=inv.id, metadata_json={"bg_number": bg.bg_number},
    )
    dispatch_hold_cfo_approval(inv)
    return inv


def release_hold(inv, user=None, comment=None):
    """Explicit release by TC Head, or an executive decline. Re-derives the
    correct non-hold stage from the actual sub-state (no stored 'previous
    stage')."""
    if inv.stage != "on_hold":
        return inv
    bg = _bg(inv)
    inv.hold_requested_by = None
    inv.stage = _derive_released_stage(inv)
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="on_hold", to_stage=inv.stage,
        action=WorkflowAction.invocation_hold_released,
        actor_id=user.id if user else None, actor_role=user.active_role if user else None,
        comments=comment or "Invocation hold released.",
    ))
    db.session.commit()
    audit_service.record(
        "invocation_hold_released", target_type="bg_invocation", target_id=inv.id,
        metadata_json={"bg_number": bg.bg_number},
    )
    if inv.signed_document_id and inv.ceo_approved_at:
        evaluate_and_send(inv)
    return inv


def _release_hold(inv, comment):
    if inv.stage != "on_hold":
        return
    release_hold(inv, comment=comment)


def _derive_released_stage(inv):
    if inv.signed_document_id:
        return "signed_uploaded"
    return "draft_generated"


def _register_targets_public():
    _register_targets()


_register_targets_public()
