import json
import logging
import re
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

from bgcc.extensions import db
from bgcc.models.audit import AuditLog
from bgcc.models.bank_verifications import BankVerification
from bgcc.models.deviations import Deviation
from bgcc.models.documents import Document
from bgcc.models.enums import BGStatus, DeviationTier, PlatformRole
from bgcc.models.generated_documents import GeneratedDocument
from bgcc.models.lifecycle import BgClosure, BgReturn, ExtensionRequest
from bgcc.models.reference import BankGuarantee, SapSystem
from bgcc.models.settings import ApplicationSetting
from bgcc.models.users import User
from bgcc.models.workflow import WorkflowHistory
from bgcc.services import (
    access_service,
    analytics_service,
    bank_verification_service,
    closure_service,
    intake_service,
    invocation_service,
    workflow_service,
)

logger = logging.getLogger("bgcc.page_context")


def _format_date(val):
    if isinstance(val, (datetime, date)):
        return val.strftime("%Y-%m-%d")
    return str(val) if val else "N/A"


def _format_amount(amount, currency="INR"):
    if amount is None:
        return "N/A"
    try:
        return f"{float(amount):,.2f} {currency or 'INR'}"
    except (ValueError, TypeError):
        return f"{amount} {currency or 'INR'}"


def _get_setting_val(key, default=None):
    s = ApplicationSetting.query.filter_by(setting_key=key).first()
    if s and s.setting_value is not None:
        return s.setting_value
    return default if default is not None else {}


def build_page_context(user, page_url=None, page_title=None, client_context=None):
    """Dynamically builds structured, authorization-checked context for the given user and page."""
    if not page_url:
        page_url = "/dashboard/"

    parsed = urlparse(page_url)
    path = parsed.path.rstrip("/")
    if not path:
        path = "/dashboard"

    query_params = parse_qs(parsed.query)
    client_ctx = client_context if isinstance(client_context, dict) else {}

    # Extract optional entity ID from client_context or query parameters
    entity_bg_id = None
    if client_ctx.get("bg_id"):
        try:
            entity_bg_id = int(client_ctx.get("bg_id"))
        except (ValueError, TypeError):
            pass
    elif query_params.get("bg_id"):
        try:
            entity_bg_id = int(query_params.get("bg_id")[0])
        except (ValueError, TypeError):
            pass

    context_data = {
        "page_url": page_url,
        "page_title": page_title or "",
        "detected_route": path,
        "user_info": {
            "name": user.full_name,
            "email": user.email,
            "active_role": user.active_role,
            "granted_roles": user.granted_roles or [],
            "business_unit": user.sap_system.display_name if user.sap_system else "All / Unassigned",
        },
        "page_details": {},
    }

    try:
        # Route dispatch
        if path in ("/dashboard", ""):
            context_data["page_details"] = _build_dashboard_context(user)
        elif path.startswith("/bg-multi-stage-approval/"):
            sub = path[len("/bg-multi-stage-approval/"):]
            if sub.isdigit():
                context_data["page_details"] = _build_approval_detail_context(user, int(sub), client_ctx)
            elif sub == "closure-verifications":
                context_data["page_details"] = _build_closure_verifications_context(user)
            else:
                context_data["page_details"] = _build_approval_queue_context(user, query_params)
        elif path == "/bg-multi-stage-approval":
            if entity_bg_id:
                context_data["page_details"] = _build_approval_detail_context(user, entity_bg_id, client_ctx)
            else:
                context_data["page_details"] = _build_approval_queue_context(user, query_params)
        elif path.startswith("/bg/"):
            sub = path[len("/bg/"):]
            if sub.isdigit():
                context_data["page_details"] = _build_bg_detail_context(user, int(sub), client_ctx)
            else:
                context_data["page_details"] = _build_general_context(user)
        elif path == "/bg-invocation":
            context_data["page_details"] = _build_invocation_context(user, entity_bg_id)
        elif path == "/bg-closure":
            context_data["page_details"] = _build_closure_context(user, entity_bg_id)
        elif path == "/bg-closure-category-lead":
            context_data["page_details"] = _build_closure_review_context(user)
        elif path == "/bg-extension":
            context_data["page_details"] = _build_extension_context(user, entity_bg_id)
        elif path == "/bg-return":
            context_data["page_details"] = _build_return_context(user, entity_bg_id)
        elif path == "/bg-status":
            context_data["page_details"] = _build_status_hub_context(user, query_params)
        elif path == "/bg-bank-tracker":
            context_data["page_details"] = _build_bank_tracker_context(user)
        elif path in ("/documents/generated", "/documents/drafts"):
            context_data["page_details"] = _build_documents_context(user, path)
        elif path in ("/bg-upload", "/bg-upload-extended"):
            context_data["page_details"] = _build_upload_context(user, path, query_params)
        elif path.startswith("/admin/configuration") or path == "/admin/configuration":
            context_data["page_details"] = _build_admin_config_context(user, query_params)
        elif path in ("/admin", "/admin/dashboard"):
            context_data["page_details"] = _build_admin_dashboard_context(user)
        elif path.startswith("/admin/users"):
            context_data["page_details"] = _build_admin_users_context(user)
        elif path.startswith("/admin/audit-log"):
            context_data["page_details"] = _build_admin_audit_context(user, query_params)
        elif path.startswith("/admin/prohibited-override"):
            sub = path.split("/")[-1]
            dev_id = int(sub) if sub.isdigit() else None
            context_data["page_details"] = _build_admin_overrides_context(user, dev_id)
        elif path.startswith("/profile"):
            context_data["page_details"] = _build_profile_context(user)
        elif path.startswith("/notifications"):
            context_data["page_details"] = _build_notifications_context(user)
        elif path.startswith("/assistant"):
            context_data["page_details"] = _build_assistant_page_context(user, client_ctx)
        else:
            context_data["page_details"] = _build_general_context(user)

    except Exception as exc:
        logger.exception("Failed to build page context for %s: %s", path, exc)
        context_data["page_details"] = _build_general_context(user)

    logger.info(
        "Built page context for user_id=%s role=%s route=%s context_type=%s",
        user.id,
        user.active_role,
        path,
        context_data["page_details"].get("context_type", "general"),
    )
    return context_data


def _build_dashboard_context(user):
    aggregates = analytics_service.get_scoped_aggregates(user) or {}
    active_bgs_query = analytics_service.get_active_bgs_query(user.sap_system_id if user.active_role != "admin" else None)
    recent_bgs = active_bgs_query.order_by(BankGuarantee.updated_at.desc()).limit(8).all()

    bg_summaries = []
    for b in recent_bgs:
        if access_service.can_view_bg(user, b):
            bg_summaries.append({
                "bg_id": b.id,
                "bg_number": b.bg_number,
                "vendor_name": b.vendor_name,
                "issuing_bank": b.issuing_bank,
                "amount": _format_amount(b.amount, b.currency),
                "expiry_date": _format_date(b.expiry_date),
                "status": b.status.value if hasattr(b.status, "value") else str(b.status),
                "current_stage": b.current_stage,
            })

    return {
        "context_type": "dashboard",
        "page_title": "Executive Dashboard & Portfolio Analytics",
        "metrics": {
            "total_active_value": _format_amount(aggregates.get("total_active_value", 0)),
            "bank_confirmed_value": _format_amount(aggregates.get("bank_confirmed_value", 0)),
            "active_bg_count": aggregates.get("active_count", 0),
            "sap_scope": aggregates.get("sap_system_name", "Company-wide"),
        },
        "breakdowns": {
            "by_bank": aggregates.get("by_bank", {}),
            "by_vendor": aggregates.get("by_vendor", {}),
            "by_business_unit": aggregates.get("by_business_unit", {}),
            "by_bg_type": aggregates.get("by_bg_type", {}),
        },
        "recent_active_bgs": bg_summaries,
    }


def _build_invocation_context(user, selected_bg_id=None):
    user_roles = set(user.granted_roles or [])
    allowed = {"bu_fc", "tc_head", "admin"}
    if user.active_role not in allowed and not (user_roles & allowed):
        return {"context_type": "invocation", "error": "User role is not authorized for BG Invocations."}

    live_bgs = invocation_service.get_live_bgs_for_user(user)
    inv_map = {b.id: invocation_service.latest_invocation(b) for b in live_bgs}

    monitor_items = []
    in_progress_items = []
    completed_items = []

    for bg in live_bgs:
        inv = inv_map.get(bg.id)
        window = invocation_service.evaluate_claim_window(bg)
        item = {
            "bg_id": bg.id,
            "bg_number": bg.bg_number,
            "vendor_name": bg.vendor_name,
            "issuing_bank": bg.issuing_bank,
            "amount": _format_amount(bg.amount, bg.currency),
            "expiry_date": _format_date(bg.expiry_date),
            "claim_expiry_date": _format_date(bg.claim_expiry_date),
            "days_until_expiry": window.get("days_until_expiry"),
            "days_until_claim_expiry": window.get("days_until_claim_expiry"),
            "is_in_claim_window": window.get("is_in_window", False),
            "is_critical": window.get("is_critical", False),
            "invocation_stage": inv.stage if inv else "none",
            "invocation_id": inv.id if inv else None,
        }
        if inv and (inv.stage == "sent_to_bank" or inv.sent_to_bank_at is not None):
            completed_items.append(item)
        elif inv and inv.stage in ("draft_generated", "signed_uploaded", "on_hold", "ceo_declined"):
            in_progress_items.append(item)
        else:
            monitor_items.append(item)

    return {
        "context_type": "invocation",
        "page_title": "BG Invocation Hub",
        "overview": {
            "critical_claim_count": sum(1 for m in monitor_items if m["is_critical"]),
            "in_claim_window_count": sum(1 for m in monitor_items if m["is_in_claim_window"]),
            "in_progress_invocations": len(in_progress_items),
            "completed_invocations": len(completed_items),
            "user_effective_role": user.active_role,
        },
        "critical_monitor_bgs": [m for m in monitor_items if m["is_critical"] or m["is_in_claim_window"]][:8],
        "in_progress_invocations_list": in_progress_items[:8],
        "available_actions": [
            "Initiate invocation for a live BG in claim window",
            "Generate formal bank invocation notice letter",
            "Upload signed CEO/CFO invocation letter",
            "Mark invocation as sent to bank",
            "Place invocation on hold / resume invocation",
        ],
    }


def _build_closure_context(user, selected_bg_id=None):
    live_bgs = BankGuarantee.query.filter_by(status=BGStatus.live.value).all()
    existing_closure_bg_ids = {
        c.bank_guarantee_id
        for c in BgClosure.query.filter(BgClosure.stage != closure_service.CLOSURE_STAGE_TERMINAL).all()
    }
    closable_bgs = [
        {
            "bg_id": b.id,
            "bg_number": b.bg_number,
            "vendor_name": b.vendor_name,
            "amount": _format_amount(b.amount, b.currency),
            "expiry_date": _format_date(b.expiry_date),
            "claim_expiry_date": _format_date(b.claim_expiry_date),
            "is_expired": bool(b.expiry_date and b.expiry_date <= date.today()),
        }
        for b in live_bgs
        if b.id not in existing_closure_bg_ids and access_service.can_view_bg(user, b)
    ]

    my_closures = (
        BgClosure.query.filter_by(initiated_by=user.id)
        .order_by(BgClosure.id.desc())
        .limit(10)
        .all()
    )
    closures_list = []
    for c in my_closures:
        bg = db.session.get(BankGuarantee, c.bank_guarantee_id)
        closures_list.append({
            "closure_id": c.id,
            "bg_id": c.bank_guarantee_id,
            "bg_number": bg.bg_number if bg else "Unknown",
            "stage": c.stage,
            "is_exception": c.is_exception,
            "justification": c.exception_justification or c.eligibility_reasoning,
            "created_at": _format_date(c.created_at),
        })

    return {
        "context_type": "closure",
        "page_title": "BG Closure Hub",
        "summary": {
            "eligible_closable_count": len(closable_bgs),
            "active_closures_count": len(closures_list),
        },
        "closable_bgs": closable_bgs[:10],
        "my_active_closures": closures_list,
        "workflow_rules": {
            "standard_closure": "Initiated when PO is fulfilled and claim expiry date has lapsed. Routes straight to ABEX verification.",
            "exception_closure": "Initiated before claim expiry or when PO is open. Requires justification and routes to Category Lead (TC Head) -> CFO -> CEO -> ABEX verification.",
            "offline_evidence": "Offline physical CFO/CEO approval document can be attached when closure is pending executive approval.",
        },
    }


def _build_closure_review_context(user):
    closures = (
        BgClosure.query.filter_by(stage="pending_category_lead")
        .order_by(BgClosure.id.asc())
        .all()
    )
    pending_list = []
    for cl in closures:
        bg = db.session.get(BankGuarantee, cl.bank_guarantee_id)
        pending_list.append({
            "closure_id": cl.id,
            "bg_id": cl.bank_guarantee_id,
            "bg_number": bg.bg_number if bg else "N/A",
            "vendor_name": bg.vendor_name if bg else "N/A",
            "amount": _format_amount(bg.amount, bg.currency) if bg else "N/A",
            "expiry_date": _format_date(bg.expiry_date) if bg else "N/A",
            "justification": cl.exception_justification or cl.eligibility_reasoning,
            "is_exception": cl.is_exception,
        })

    return {
        "context_type": "closure_review",
        "page_title": "Closure Review Workspace (Category Lead / TC Head)",
        "pending_reviews_count": len(pending_list),
        "pending_closures": pending_list,
        "available_actions": ["Approve exception closure (advances to CFO email magic-link)", "Reject closure with required comment"],
    }


def _build_extension_context(user, selected_bg_id=None):
    live_bgs = BankGuarantee.query.filter_by(status=BGStatus.live.value).all()
    reqs = ExtensionRequest.query.order_by(ExtensionRequest.id.desc()).all()
    req_by_bg = {r.parent_bg_id: r for r in reqs}

    today = date.today()
    sections = {"approaching": [], "requested": [], "overdue": [], "completed": []}

    for bg in live_bgs:
        if not access_service.can_view_bg(user, bg):
            continue
        req = req_by_bg.get(bg.id)
        days = (bg.expiry_date - today).days if bg.expiry_date else 999
        row = {
            "bg_id": bg.id,
            "bg_number": bg.bg_number,
            "vendor_name": bg.vendor_name,
            "amount": _format_amount(bg.amount, bg.currency),
            "expiry_date": _format_date(bg.expiry_date),
            "days_to_expiry": days,
            "extension_status": req.stage if req else "none",
            "vendor_email": req.vendor_email if req else (bg.vendor_name and bg.vendor_name.lower().replace(" ", ".") + "@bg.center" or ""),
        }
        if req is None:
            sections["approaching"].append(row)
        elif req.stage in ("not_started", "requested"):
            if req.is_overdue or days < 0:
                sections["overdue"].append(row)
            else:
                sections["requested"].append(row)
        else:
            sections["completed"].append(row)

    for k in sections:
        sections[k].sort(key=lambda r: r["days_to_expiry"])

    return {
        "context_type": "extension",
        "page_title": "BG Extension Workspace",
        "counts": {
            "approaching_expiry": len(sections["approaching"]),
            "requested": len(sections["requested"]),
            "overdue": len(sections["overdue"]),
            "completed": len(sections["completed"]),
        },
        "approaching_bgs": sections["approaching"][:6],
        "overdue_bgs": sections["overdue"][:6],
        "requested_bgs": sections["requested"][:6],
        "actions": ["Initiate extension request to vendor", "Upload replacement/extended BG document against parent BG"],
    }


def _build_return_context(user, selected_bg_id=None):
    query = BankGuarantee.query.filter(
        BankGuarantee.status.in_([BGStatus.live.value, BGStatus.closed.value])
    )
    if user.active_role == "creator":
        query = query.filter(BankGuarantee.creator_id == user.id)

    bgs = query.all()
    bgs_map = {b.id: b for b in bgs if access_service.can_view_bg(user, b)}
    returns = BgReturn.query.filter(BgReturn.bank_guarantee_id.in_(bgs_map.keys() or [0])).all()

    return_records = []
    for r in returns:
        bg = bgs_map.get(r.bank_guarantee_id)
        return_records.append({
            "return_id": r.id,
            "bg_id": r.bank_guarantee_id,
            "bg_number": bg.bg_number if bg else "N/A",
            "vendor_name": bg.vendor_name if bg else "N/A",
            "status": r.status,
            "dispatch_id": r.dispatch_id,
            "receipt_confirmed_at": _format_date(r.receipt_confirmed_at) if r.receipt_confirmed_at else None,
        })

    eligible_bgs = [
        {
            "bg_id": b.id,
            "bg_number": b.bg_number,
            "vendor_name": b.vendor_name,
            "status": b.status.value if hasattr(b.status, "value") else str(b.status),
            "expiry_date": _format_date(b.expiry_date),
        }
        for b in bgs_map.values()
    ]

    return {
        "context_type": "return",
        "page_title": "BG Physical Return Hub",
        "summary": {
            "eligible_for_return": len(eligible_bgs),
            "active_return_requests": len(return_records),
        },
        "active_returns": return_records[:8],
        "eligible_bgs_summary": eligible_bgs[:8],
        "workflow_steps": [
            "1. Request return of physical original BG to vendor",
            "2. Dispatch physical copy via Courier (tracking number) or CMR (deliverer details)",
            "3. Confirm physical receipt by vendor / acknowledgement receipt",
        ],
    }


def _build_approval_detail_context(user, bg_id, client_ctx=None):
    bg = db.session.get(BankGuarantee, bg_id)
    if not bg or not access_service.can_view_bg(user, bg):
        return {"context_type": "approval_detail", "error": f"Bank Guarantee #{bg_id} not found or access denied."}

    role = user.active_role
    stage_info = workflow_service.stage_info(bg)
    analysis = intake_service.primary_analysis(bg)
    fields = (analysis.extracted_fields or {}) if analysis else {}
    po_result = intake_service.po_cross_check_result(bg) or {}
    checklist = intake_service.format_checklist(bg) or []
    deviations = intake_service.all_deviations_for(bg.id)

    dev_list = []
    for d in deviations:
        dev_list.append({
            "deviation_id": d.id,
            "clause_reference": d.clause_reference,
            "deviation_type": d.deviation_type,
            "ai_proposed_tier": d.ai_proposed_tier.value if hasattr(d.ai_proposed_tier, "value") else str(d.ai_proposed_tier or "low"),
            "effective_tier": d.effective_tier.value if hasattr(d.effective_tier, "value") else str(d.effective_tier or "low"),
            "status": d.status.value if hasattr(d.status, "value") else str(d.status or "pending"),
            "decision_comment": d.decision_comment,
            "excerpt": (d.bg_text_excerpt or "")[:200],
            "admin_overridden": bool(d.admin_override_by),
        })

    chk_list = []
    for c in checklist:
        chk_list.append({
            "key": c.get("key"),
            "label": c.get("label"),
            "passed": c.get("passed"),
            "mandatory": c.get("mandatory"),
            "reason": c.get("reason"),
        })

    prior_events = (
        WorkflowHistory.query.filter_by(bank_guarantee_id=bg.id)
        .order_by(WorkflowHistory.created_at.desc())
        .limit(6)
        .all()
    )
    history_list = [
        {
            "from_stage": h.from_stage,
            "to_stage": h.to_stage,
            "action": h.action.value if hasattr(h.action, "value") else str(h.action),
            "actor_role": h.actor_role,
            "comments": h.comments,
            "timestamp": _format_date(h.created_at),
        }
        for h in prior_events
    ]

    is_user_authorized = stage_info.get("authorized_role") == role or "admin" in (user.granted_roles or [])

    return {
        "context_type": "approval_detail",
        "page_title": f"Review Workspace: {bg.bg_number}",
        "bg_details": {
            "bg_id": bg.id,
            "bg_number": bg.bg_number,
            "vendor_name": bg.vendor_name,
            "issuing_bank": bg.issuing_bank,
            "amount": _format_amount(bg.amount, bg.currency),
            "issue_date": _format_date(bg.issue_date),
            "expiry_date": _format_date(bg.expiry_date),
            "claim_expiry_date": _format_date(bg.claim_expiry_date),
            "expenditure_type": bg.expenditure_type,
            "status": bg.status.value if hasattr(bg.status, "value") else str(bg.status),
            "current_stage": bg.current_stage,
            "highest_deviation_tier": workflow_service.highest_tier(bg),
            "requires_ceo_cfo": workflow_service.requires_ceo_cfo(bg),
        },
        "workflow_state": {
            "current_stage": bg.current_stage,
            "authorized_role_for_stage": stage_info.get("authorized_role"),
            "next_role": stage_info.get("next_role"),
            "is_final_stage": stage_info.get("is_final"),
            "user_can_decide_now": is_user_authorized,
        },
        "deviations_count": len(dev_list),
        "deviations": dev_list,
        "format_checklist": chk_list,
        "po_cross_check": {
            "po_number": po_result.get("po_number"),
            "po_vendor": po_result.get("po_vendor"),
            "similarity_passed": po_result.get("similar"),
            "amount_match": po_result.get("amount_match"),
            "explanation": po_result.get("explanation"),
        },
        "recent_workflow_history": history_list,
    }


def _build_approval_queue_context(user, query_params=None):
    role = user.active_role
    status = workflow_service.queue_role_status(role)
    bgs_in_queue = []
    if status:
        query = BankGuarantee.query.filter(
            db.or_(BankGuarantee.status == status, BankGuarantee.current_stage == status.value)
        )
        if user.sap_system_id and "admin" not in (user.granted_roles or []):
            query = query.filter(BankGuarantee.sap_system_id == user.sap_system_id)
        for b in query.order_by(BankGuarantee.created_at.desc()).limit(12).all():
            if access_service.can_view_bg(user, b):
                bgs_in_queue.append({
                    "bg_id": b.id,
                    "bg_number": b.bg_number,
                    "vendor_name": b.vendor_name,
                    "issuing_bank": b.issuing_bank,
                    "amount": _format_amount(b.amount, b.currency),
                    "expenditure_type": b.expenditure_type,
                    "highest_tier": workflow_service.highest_tier(b),
                    "current_stage": b.current_stage,
                    "expiry_date": _format_date(b.expiry_date),
                })

    return {
        "context_type": "approval_queue",
        "page_title": f"Multi-Stage Approval Queue ({role.replace('_', ' ').title() if role else 'Queue'})",
        "queue_status_target": status.value if hasattr(status, "value") else str(status),
        "queue_items_count": len(bgs_in_queue),
        "queue_bgs": bgs_in_queue,
    }


def _build_closure_verifications_context(user):
    closures = (
        BgClosure.query.filter_by(stage="pending_abex_verification")
        .order_by(BgClosure.id.asc())
        .all()
    )
    items = []
    for cl in closures:
        bg = db.session.get(BankGuarantee, cl.bank_guarantee_id)
        items.append({
            "closure_id": cl.id,
            "bg_id": cl.bank_guarantee_id,
            "bg_number": bg.bg_number if bg else "N/A",
            "vendor_name": bg.vendor_name if bg else "N/A",
            "amount": _format_amount(bg.amount, bg.currency) if bg else "N/A",
            "stage": cl.stage,
            "is_exception": cl.is_exception,
        })
    return {
        "context_type": "closure_verifications",
        "page_title": "ABEX Closure Verifications Queue",
        "pending_count": len(items),
        "closures": items,
    }


def _build_bg_detail_context(user, bg_id, client_ctx=None):
    bg = db.session.get(BankGuarantee, bg_id)
    if not bg or not access_service.can_view_bg(user, bg):
        return {"context_type": "bg_detail", "error": f"Bank Guarantee #{bg_id} not found or unauthorized."}

    analysis = intake_service.primary_analysis(bg)
    fields = (analysis.extracted_fields or {}) if analysis else {}
    po_result = intake_service.po_cross_check_result(bg) or {}
    deviations = intake_service.all_deviations_for(bg.id)
    verif = bank_verification_service.verification_for(bg)
    sap = db.session.get(SapSystem, bg.sap_system_id) if bg.sap_system_id else None

    return {
        "context_type": "bg_detail",
        "page_title": f"Bank Guarantee: {bg.bg_number}",
        "record": {
            "bg_id": bg.id,
            "bg_number": bg.bg_number,
            "vendor_name": bg.vendor_name,
            "issuing_bank": bg.issuing_bank,
            "amount": _format_amount(bg.amount, bg.currency),
            "currency": bg.currency,
            "issue_date": _format_date(bg.issue_date),
            "expiry_date": _format_date(bg.expiry_date),
            "claim_expiry_date": _format_date(bg.claim_expiry_date),
            "status": bg.status.value if hasattr(bg.status, "value") else str(bg.status),
            "current_stage": bg.current_stage,
            "expenditure_type": bg.expenditure_type,
            "sap_system": sap.display_name if sap else "N/A",
        },
        "bank_verification_status": verif.status if verif else "untracked",
        "deviations_summary": {
            "total": len(deviations),
            "highest_tier": workflow_service.highest_tier(bg),
            "list": [
                {"clause": d.clause_reference, "tier": d.effective_tier.value if hasattr(d.effective_tier, "value") else str(d.effective_tier or "low"), "status": d.status.value if hasattr(d.status, "value") else str(d.status or "pending")}
                for d in deviations[:5]
            ],
        },
        "po_cross_check": {
            "po_number": po_result.get("po_number"),
            "po_vendor": po_result.get("po_vendor"),
            "amount_match": po_result.get("amount_match"),
        },
    }


def _build_status_hub_context(user, query_params=None):
    q = query_params or {}
    status_filter = q.get("status", [None])[0]
    vendor_filter = q.get("vendor", [None])[0]
    bu_filter = q.get("business_unit", [None])[0]

    query = BankGuarantee.query
    if status_filter:
        query = query.filter(BankGuarantee.status == status_filter)
    if vendor_filter:
        query = query.filter(BankGuarantee.vendor_name.ilike(f"%{vendor_filter}%"))

    total = query.count()
    sample_bgs = query.order_by(BankGuarantee.updated_at.desc()).limit(10).all()
    scoped_sample = [
        {
            "bg_id": b.id,
            "bg_number": b.bg_number,
            "vendor_name": b.vendor_name,
            "issuing_bank": b.issuing_bank,
            "amount": _format_amount(b.amount, b.currency),
            "status": b.status.value if hasattr(b.status, "value") else str(b.status),
            "expiry_date": _format_date(b.expiry_date),
        }
        for b in sample_bgs
        if access_service.can_view_bg(user, b)
    ]

    return {
        "context_type": "status_hub",
        "page_title": "BG Status Hub & Repository Search",
        "active_filters": {
            "status": status_filter,
            "vendor": vendor_filter,
            "business_unit": bu_filter,
        },
        "total_matching_bgs": total,
        "sample_matching_bgs": scoped_sample,
    }


def _build_bank_tracker_context(user):
    bgs = BankGuarantee.query.order_by(BankGuarantee.updated_at.desc()).limit(15).all()
    scoped_bgs = [b for b in bgs if access_service.can_view_bg(user, b)]

    verifs = []
    for b in scoped_bgs:
        v = bank_verification_service.verification_for(b)
        verifs.append({
            "bg_id": b.id,
            "bg_number": b.bg_number,
            "issuing_bank": b.issuing_bank,
            "verification_status": v.status if v else "not_initiated",
            "bank_reference": v.response_reference if v else None,
            "sent_at": _format_date(v.sent_at) if (v and v.sent_at) else None,
            "confirmed_at": _format_date(v.confirmed_at) if (v and v.confirmed_at) else None,
        })

    return {
        "context_type": "bank_tracker",
        "page_title": "Bank Verification Tracker",
        "tracked_bgs_count": len(verifs),
        "verifications_summary": verifs,
        "actions": ["Re-send bank verification email", "Manually update bank confirmation / rejection status with reference"],
    }


def _build_documents_context(user, path):
    if path == "/documents/generated":
        docs = (
            GeneratedDocument.query.order_by(GeneratedDocument.created_at.desc())
            .limit(10)
            .all()
        )
        doc_list = []
        for d in docs:
            bg = db.session.get(BankGuarantee, d.bank_guarantee_id)
            if bg and access_service.can_view_bg(user, bg):
                doc_list.append({
                    "doc_id": d.id,
                    "bg_number": bg.bg_number,
                    "document_type": d.document_type,
                    "version": d.version,
                    "created_at": _format_date(d.created_at),
                })
        return {
            "context_type": "generated_documents",
            "page_title": "Generated Documents Repository",
            "documents": doc_list,
        }
    else:
        # Saved Drafts
        query = BankGuarantee.query.filter_by(status=BGStatus.draft)
        if user.active_role == "creator":
            query = query.filter_by(creator_id=user.id)
        elif user.active_role == "coordinator":
            query = query.filter_by(coordinator_id=user.id)
        drafts = [
            {
                "bg_id": b.id,
                "vendor_name": b.vendor_name,
                "issuing_bank": b.issuing_bank,
                "amount": _format_amount(b.amount, b.currency),
                "created_at": _format_date(b.created_at),
            }
            for b in query.limit(8).all()
        ]
        return {
            "context_type": "drafts",
            "page_title": "Saved BG Drafts",
            "drafts": drafts,
        }


def _build_upload_context(user, path, query_params=None):
    is_extension = "extended" in path or (query_params and "parent" in query_params)
    return {
        "context_type": "upload_intake",
        "page_title": "Upload Extended Bank Guarantee" if is_extension else "Upload New Bank Guarantee",
        "is_extension_upload": is_extension,
        "intake_guidelines": {
            "accepted_formats": "PDF files up to 20MB (new) / 10MB (extended)",
            "multimodal_extraction": "Gemini AI extracts 10 core fields, full clause text, and runs format checklist",
            "deviations_engine": "Compares clauses against master template (new) or parent BG (extension)",
            "cross_checks": "Performs SAP PO number matching and vendor similarity check",
        },
    }


def _build_admin_config_context(user, query_params=None):
    if "admin" not in (user.granted_roles or []) and user.active_role != "admin":
        return {"context_type": "admin_config", "error": "Admin access required."}

    doa = _get_setting_val("doa_matrix")
    clause_tpl = _get_setting_val("active_clause_template")
    patterns = _get_setting_val("prohibited_clause_patterns", [])
    checklist = _get_setting_val("checklist_definitions")
    banks = _get_setting_val("approved_banks")
    lifecycle_policy = {
        "extension": _get_setting_val("extension_policy"),
        "invocation": _get_setting_val("invocation_policy"),
        "contacts": _get_setting_val("executive_contacts"),
    }
    sap_systems = [
        {"code": s.code, "display_name": s.display_name, "business_unit": s.business_unit, "active": s.is_active}
        for s in SapSystem.query.all()
    ]

    return {
        "context_type": "admin_configuration",
        "page_title": "Platform Configuration & Master Settings",
        "doa_matrix": {
            "stage_sequence": doa.get("stage_sequence", []),
            "ceo_cfo_condition": doa.get("ceo_cfo_condition", {}),
            "deviation_visibility": doa.get("deviation_visibility", {}),
        },
        "clause_template": {
            "name": clause_tpl.get("name"),
            "mandatory_clauses": clause_tpl.get("mandatory_clauses", []),
            "clauses_count": len(clause_tpl.get("clauses", [])),
        },
        "prohibited_patterns_count": len(patterns) if isinstance(patterns, list) else 0,
        "checklist_definitions_count": len(checklist.get("items", [])) if isinstance(checklist, dict) else 0,
        "approved_banks_count": len(banks.get("banks", [])) if isinstance(banks, dict) else 0,
        "lifecycle_policies": lifecycle_policy,
        "business_units_count": len(sap_systems),
        "business_units": sap_systems,
    }


def _build_admin_dashboard_context(user):
    if "admin" not in (user.granted_roles or []) and user.active_role != "admin":
        return {"context_type": "admin_dashboard", "error": "Admin access required."}

    pending_users = User.query.filter_by(is_approved=False, is_active=True).count()
    pending_overrides = Deviation.query.filter(
        Deviation.effective_tier == "prohibited",
        Deviation.admin_override_by.is_(None),
    ).count()

    return {
        "context_type": "admin_dashboard",
        "page_title": "Platform Administration Dashboard",
        "pending_user_approvals": pending_users,
        "pending_prohibited_overrides": pending_overrides,
        "admin_capabilities": [
            "Approve/reject newly registered user accounts and assign roles",
            "Review and override prohibited clause deviations with written justification",
            "Configure DoA matrices, clause templates, and approved banks",
            "Inspect full audit logs across the platform",
        ],
    }


def _build_admin_users_context(user):
    if "admin" not in (user.granted_roles or []) and user.active_role != "admin":
        return {"context_type": "admin_users", "error": "Admin access required."}

    pending = User.query.filter_by(is_approved=False, is_active=True).all()
    active_count = User.query.filter_by(is_approved=True, is_active=True).count()

    return {
        "context_type": "admin_users",
        "page_title": "User and Role Management",
        "active_users_count": active_count,
        "pending_registrations_count": len(pending),
        "pending_users": [
            {"id": u.id, "email": u.email, "name": u.full_name, "created_at": _format_date(u.created_at)}
            for u in pending[:6]
        ],
    }


def _build_admin_audit_context(user, query_params=None):
    if "admin" not in (user.granted_roles or []) and user.active_role != "admin":
        return {"context_type": "admin_audit", "error": "Admin access required."}

    recent_logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(10).all()
    return {
        "context_type": "admin_audit_log",
        "page_title": "System Audit Log Viewer",
        "recent_audit_events": [
            {
                "event_type": l.event_type,
                "target_type": l.target_type,
                "target_id": l.target_id,
                "actor_id": l.actor_id,
                "timestamp": _format_date(l.created_at),
            }
            for l in recent_logs
        ],
    }


def _build_admin_overrides_context(user, dev_id=None):
    if "admin" not in (user.granted_roles or []) and user.active_role != "admin":
        return {"context_type": "admin_overrides", "error": "Admin access required."}

    if dev_id:
        d = db.session.get(Deviation, dev_id)
        if d:
            bg = db.session.get(BankGuarantee, d.bank_guarantee_id)
            return {
                "context_type": "admin_override_detail",
                "page_title": f"Prohibited Override for Clause {d.clause_reference}",
                "deviation": {
                    "id": d.id,
                    "bg_number": bg.bg_number if bg else "N/A",
                    "clause_reference": d.clause_reference,
                    "excerpt": d.bg_text_excerpt,
                    "is_overridden": bool(d.admin_override_by),
                },
            }

    pending_devs = (
        Deviation.query.filter(
            Deviation.effective_tier == "prohibited",
            Deviation.admin_override_by.is_(None),
        )
        .limit(10)
        .all()
    )
    return {
        "context_type": "admin_prohibited_overrides",
        "page_title": "Prohibited Clause Overrides",
        "pending_count": len(pending_devs),
        "pending_overrides": [
            {"id": d.id, "bg_id": d.bank_guarantee_id, "clause": d.clause_reference, "excerpt": (d.bg_text_excerpt or "")[:120]}
            for d in pending_devs
        ],
    }


def _build_profile_context(user):
    return {
        "context_type": "profile",
        "page_title": "User Profile & Preferences",
        "profile": {
            "name": user.full_name,
            "email": user.email,
            "active_role": user.active_role,
            "granted_roles": user.granted_roles or [],
            "business_unit": user.sap_system.display_name if user.sap_system else "None",
            "is_multi_role": user.is_multi_role,
        },
    }


def _build_notifications_context(user):
    from bgcc.models.notifications import Notification
    unread = (
        Notification.query.filter_by(user_id=user.id, is_read=False)
        .order_by(Notification.created_at.desc())
        .limit(5)
        .all()
    )
    return {
        "context_type": "notifications",
        "page_title": "User Notifications Center",
        "unread_count": len(unread),
        "recent_unread": [
            {"title": n.title, "body": n.body, "link_url": n.link_url, "created_at": _format_date(n.created_at)}
            for n in unread
        ],
    }


def _build_assistant_page_context(user, client_ctx=None):
    # When on /assistant/, if the user or sessionStorage provided a previous context URL or entity, resolve that!
    source_url = client_ctx.get("source_url") or client_ctx.get("last_page_url") if client_ctx else None
    if source_url and source_url not in ("/assistant", "/assistant/"):
        return build_page_context(user, page_url=source_url, client_context=client_ctx).get("page_details", {})

    return _build_general_context(user)


def _build_general_context(user):
    live_count = BankGuarantee.query.filter(
        BankGuarantee.status.in_([BGStatus.live.value, "live", "approved"])
    ).count()

    return {
        "context_type": "general",
        "page_title": "BG Command Centre Application",
        "user_state": {
            "name": user.full_name,
            "role": user.active_role,
            "business_unit": user.sap_system.display_name if user.sap_system else "Company-wide",
        },
        "live_bgs_in_scope": live_count,
    }


def format_context_for_prompt(context_dict):
    """Converts the structured context dictionary into a clean markdown block for Gemini prompt."""
    page_details = context_dict.get("page_details", {})
    user_info = context_dict.get("user_info", {})
    route = context_dict.get("detected_route", "")
    page_title = context_dict.get("page_title") or page_details.get("page_title", route)

    lines = [
        f"### CURRENT APPLICATION & WORKFLOW CONTEXT",
        f"- **Current Page / Screen**: {page_title} (`{route}`)",
        f"- **Logged-in User**: {user_info.get('name')} ({user_info.get('email')})",
        f"- **Active Role**: {user_info.get('active_role')}",
        f"- **Business Unit / SAP Scope**: {user_info.get('business_unit')}",
        "",
        "#### Live Page Data & State:",
        json.dumps(page_details, indent=2, default=str),
    ]
    return "\n".join(lines)
