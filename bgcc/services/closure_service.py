"""BG closure & physical return business logic (Step 4).

Includes the deterministic eligibility engine, the sequential CFO-then-CEO
executive-approval wiring (via the generic magic-link service), and the ABEX
segregation-of-duties rule.
"""
from datetime import datetime

from bgcc.extensions import db
from bgcc.models.enums import WorkflowAction
from bgcc.models.lifecycle import BgClosure
from bgcc.models.workflow import WorkflowHistory
from bgcc.services import audit_service, magic_link_service, sap_service
from bgcc.services.intake_service import parse_money

CLOSURE_STAGE_TERMINAL = "closed"


def compute_eligibility(bg):
    """Live-computed standard vs exception eligibility.

    Standard: the underlying PO/contract is fully executed AND (for an ABG) the
    open advance is zero. Everything else is an exception. Never read from a
    stored/stale value. Raises ValueError if the SAP call fails.
    """
    po_numbers = bg.po_numbers or []
    po_context = sap_service.get_po_context(po_numbers) if po_numbers else []
    po_executed = all(c.get("is_executed") for c in po_context) if po_context else False

    is_abg = bg.bg_type == "abg"
    total_open = sum(
        int(parse_money(c.get("open_advance_amount")) or 0)
        for c in po_context
    ) if po_context else 0

    standard = bool(po_executed) and (not is_abg or total_open == 0)
    if po_context:
        po_list = ", ".join(c["po_number"] for c in po_context)
    else:
        po_list = ", ".join(po_numbers) or "n/a"

    lines = [
        f"Underlying PO/contract ({po_list}) fully executed: {'Yes' if po_executed else 'No'}.",
    ]
    if is_abg:
        lines.append(f"Open advance amount: {total_open}.")
        lines.append("Advance fully recovered" if total_open == 0 else "Advance still open.")
    reasoning = " ".join(lines)
    reasoning += (
        " Closure is STANDARD (proceeds directly to ABEX verification)."
        if standard
        else " Closure is an EXCEPTION and requires category-lead review and executive sign-off."
    )
    return {
        "standard": standard,
        "po_executed": po_executed,
        "is_abg": is_abg,
        "total_open_advance": str(total_open),
        "reasoning": reasoning,
    }


def initiate_closure(bg, user, justification=None):
    """Create a bg_closures row and route standard/exception accordingly."""
    eligibility = compute_eligibility(bg)
    is_exception = not eligibility["standard"]
    if is_exception and not (justification or "").strip():
        raise ValueError("Please provide a justification for this exception closure.")

    closure = BgClosure(
        bank_guarantee_id=bg.id,
        is_exception=is_exception,
        eligibility_reasoning=eligibility["reasoning"],
        exception_justification=(justification or "").strip() or None,
        stage="pending_category_lead" if is_exception else "pending_abex_verification",
        initiated_by=user.id,
    )
    db.session.add(closure)
    db.session.flush()
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id,
        from_stage=bg.status.value,
        to_stage=closure.stage,
        action=WorkflowAction.closure_initiated,
        actor_id=user.id,
        actor_role=user.active_role,
        comments=eligibility["reasoning"],
    ))
    db.session.commit()
    audit_service.record(
        "closure_initiated", actor_id=user.id, target_type="bg_closure",
        target_id=closure.id,
        metadata_json={"bg_number": bg.bg_number, "is_exception": is_exception,
                       "stage": closure.stage},
    )
    return closure


def active_closure_for(bg_id):
    return (
        BgClosure.query.filter_by(bank_guarantee_id=bg_id)
        .order_by(BgClosure.id.desc())
        .first()
    )


def is_standard_or_terminal(closure):
    return closure.stage in (CLOSURE_STAGE_TERMINAL, "pending_abex_verification")


# ------------------------------------------------------------------ magic links

def _cfo_email():
    from bgcc.models.settings import ApplicationSetting

    setting = ApplicationSetting.query.filter_by(setting_key="executive_contacts").first()
    value = (setting.setting_value if setting else {}) or {}
    return value.get("cfo_email") or value.get("cfo")


def _ceo_email():
    from bgcc.models.settings import ApplicationSetting

    setting = ApplicationSetting.query.filter_by(setting_key="executive_contacts").first()
    value = (setting.setting_value if setting else {}) or {}
    return value.get("ceo_email") or value.get("ceo")


def _bg(closure):
    from bgcc.models.reference import BankGuarantee

    return db.session.get(BankGuarantee, closure.bank_guarantee_id)


def _on_cfo_approve(record):
    bg = _bg(record)
    record.stage = "pending_ceo"
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="pending_cfo", to_stage="pending_ceo",
        action=WorkflowAction.executive_approved, actor_role="cfo",
        comments="CFO approved closure via magic link.",
    ))
    db.session.commit()
    audit_service.record(
        "executive_approved", target_type="bg_closure", target_id=record.id,
        metadata_json={"bg_number": bg.bg_number, "role": "cfo", "closure_id": record.id},
    )
    # Sequential: only now dispatch the CEO email.
    dispatch_ceo_approval(record)


def _on_ceo_approve(record):
    # Sequential gate: CEO approval must never happen before CFO approval.
    if record.cfo_approved_at is None:
        raise ValueError("CFO approval must be recorded before CEO approval.")
    bg = _bg(record)
    record.stage = "pending_abex_verification"
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="pending_ceo",
        to_stage="pending_abex_verification",
        action=WorkflowAction.executive_approved, actor_role="ceo",
        comments="CEO approved closure via magic link.",
    ))
    db.session.commit()
    audit_service.record(
        "executive_approved", target_type="bg_closure", target_id=record.id,
        metadata_json={"bg_number": bg.bg_number, "role": "ceo", "closure_id": record.id},
    )


def _on_cfo_decline(record):
    _terminate(record, "CFO declined closure via magic link.", "cfo")


def _on_ceo_decline(record):
    _terminate(record, "CEO declined closure via magic link.", "ceo")


def _terminate(record, comment, role):
    bg = _bg(record)
    record.stage = CLOSURE_STAGE_TERMINAL
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage=record.stage or "pending",
        to_stage=CLOSURE_STAGE_TERMINAL, action=WorkflowAction.executive_declined,
        actor_role=role, comments=comment,
    ))
    db.session.commit()
    audit_service.record(
        "executive_declined", target_type="bg_closure", target_id=record.id,
        metadata_json={"bg_number": bg.bg_number, "role": role},
    )


def _register_targets():
    if _register_targets.done:
        return
    magic_link_service.register_target(
        key="closure_cfo",
        model=BgClosure,
        token_attr="cfo_approval_token",
        ts_attr="cfo_approved_at",
        salt="closure-cfo-approval",
        max_age_settings_key="executive_approval_expiry_hours",
        max_age_default=72 * 3600,
        title="CFO approval required - Bank Guarantee closure",
        subject="A Bank Guarantee closure requires your CFO approval",
        on_approve=_on_cfo_approve,
        on_decline=_on_cfo_decline,
    )
    magic_link_service.register_target(
        key="closure_ceo",
        model=BgClosure,
        token_attr="ceo_approval_token",
        ts_attr="ceo_approved_at",
        salt="closure-ceo-approval",
        max_age_settings_key="executive_approval_expiry_hours",
        max_age_default=72 * 3600,
        title="CEO approval required - Bank Guarantee closure",
        subject="A Bank Guarantee closure requires your CEO approval",
        on_approve=_on_ceo_approve,
        on_decline=_on_ceo_decline,
    )
    _register_targets.done = True


_register_targets.done = False


def dispatch_cfo_approval(closure):
    _register_targets()
    email = _cfo_email()
    if not email:
        raise ValueError("No CFO contact email is configured.")
    bg = _bg(closure)
    magic_link_service.issue_and_email(
        "closure_cfo", closure, email,
        extra_lines=[
            f"Bank Guarantee: {bg.bg_number}",
            f"Vendor: {bg.vendor_name or 'n/a'}",
            f"Reason: {closure.eligibility_reasoning}",
        ],
    )


def dispatch_ceo_approval(closure):
    _register_targets()
    email = _ceo_email()
    if not email:
        raise ValueError("No CEO contact email is configured.")
    bg = _bg(closure)
    magic_link_service.issue_and_email(
        "closure_ceo", closure, email,
        extra_lines=[
            f"Bank Guarantee: {bg.bg_number}",
            f"Vendor: {bg.vendor_name or 'n/a'}",
            f"Reason: {closure.eligibility_reasoning}",
        ],
    )


def can_abex_verify(closure, user):
    """Unconditional segregation-of-duties check."""
    if closure.initiated_by == user.id:
        return False, "You cannot verify a closure you initiated (segregation of duties)."
    bg = _bg(closure)
    reviewer = (
        WorkflowHistory.query.filter_by(
            bank_guarantee_id=bg.id, action=WorkflowAction.closure_reviewed.value
        ).order_by(WorkflowHistory.id.desc()).first()
    )
    if reviewer and reviewer.actor_id == user.id:
        return False, "You cannot verify a closure you reviewed as TC Head (segregation of duties)."
    return True, None


def verify_closure(closure, user):
    ok, reason = can_abex_verify(closure, user)
    if not ok:
        raise PermissionError(reason)
    bg = _bg(closure)
    from bgcc.models.enums import BGStatus

    closure.verified_by = user.id
    closure.closed_at = datetime.utcnow()
    closure.stage = CLOSURE_STAGE_TERMINAL
    bg.status = BGStatus.closed
    bg.current_stage = BGStatus.closed.value
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="pending_abex_verification",
        to_stage=CLOSURE_STAGE_TERMINAL, action=WorkflowAction.closure_verified,
        actor_id=user.id, actor_role=user.active_role,
        comments=closure.eligibility_reasoning,
    ))
    db.session.commit()
    audit_service.record(
        "closure_verified", actor_id=user.id, target_type="bg_closure",
        target_id=closure.id,
        metadata_json={"bg_number": bg.bg_number, "closed_at": str(closure.closed_at)},
    )
    return closure


# Register the closure magic-link targets at import time so the public
# verification route can resolve them without a prior dispatch.
_register_targets()
