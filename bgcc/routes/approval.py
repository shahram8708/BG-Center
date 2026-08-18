from datetime import datetime

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from bgcc.extensions import db, limiter
from bgcc.models.audit import AuditLog
from bgcc.models.documents import Document
from bgcc.models.enums import BGStatus, PlatformRole, WorkflowAction
from bgcc.models.lifecycle import BgClosure
from bgcc.models.reference import BankGuarantee, SapSystem
from bgcc.models.saved_views import SavedView
from bgcc.models.workflow import WorkflowHistory
from bgcc.services import (
    access_service,
    audit_service,
    closure_service,
    intake_service,
    workflow_service,
)
from bgcc.services.prohibited_clauses import effective_tier
from bgcc.tasks.workflow_tasks import enqueue_fanout

bp = Blueprint("approval", __name__, url_prefix="")

_QUEUE_ROLES = workflow_service.QUEUE_ROLES
_DEV_DECISION = {"accepted", "rejected"}


# ------------------------------------------------------------------ helpers

def _authorized_for_stage(bg):
    """Return the authorized role for the BG's current stage, or None."""
    role = workflow_service.current_authorized_role(bg)
    if role is None:
        return None
    return role


def _require_stage_access(bg):
    role = current_user.active_role
    authorized = _authorized_for_stage(bg)
    if role != authorized:
        abort(403)
    if role in workflow_service.FINANCE_ROLES:
        expected = "bu_cfmc" if bg.expenditure_type == "capex" else "bu_fc"
        if role != expected:
            abort(403)
    if not access_service.can_view_bg(current_user, bg):
        abort(403)
    return role


def _visible(bg, role):
    return workflow_service.visible_deviations(bg, role)


def _prior_events(bg):
    events = AuditLog.query.filter(
        AuditLog.target_type == "bank_guarantee",
        AuditLog.target_id == str(bg.id),
        AuditLog.event_type.in_(["deviation_decision", "deviation_tier_changed"]),
    ).order_by(AuditLog.created_at).all()
    history = WorkflowHistory.query.filter_by(bank_guarantee_id=bg.id).order_by(
        WorkflowHistory.created_at
    ).all()
    return events, history


# ------------------------------------------------------------------- queue

@bp.route("/bg-multi-stage-approval")
@login_required
def queue():
    role = current_user.active_role
    if role not in _QUEUE_ROLES:
        abort(403)
    status = workflow_service.queue_role_status(role)

    sap_system_id = request.args.get("sap", type=int)
    min_amount = request.args.get("min_amount", type=float)
    max_amount = request.args.get("max_amount", type=float)
    view_id = request.args.get("view", type=int)

    if view_id:
        saved = SavedView.query.filter_by(id=view_id, user_id=current_user.id).first()
        if saved:
            state = saved.filter_state or {}
            sap_system_id = state.get("sap")
            min_amount = state.get("min_amount")
            max_amount = state.get("max_amount")

    query = BankGuarantee.query.filter_by(status=status)
    if sap_system_id:
        query = query.filter(BankGuarantee.sap_system_id == sap_system_id)
    if min_amount is not None:
        query = query.filter(BankGuarantee.amount >= min_amount)
    if max_amount is not None:
        query = query.filter(BankGuarantee.amount <= max_amount)
    query = query.order_by(BankGuarantee.created_at.asc())
    page = query.paginate(page=request.args.get("page", 1, type=int), per_page=10, error_out=False)

    days_pending = {}
    for bg in page.items:
        days_pending[bg.id] = (datetime.utcnow() - bg.created_at).days

    saved_views = SavedView.query.filter_by(
        user_id=current_user.id, page_key="approval_queue"
    ).order_by(SavedView.name).all()
    sap_systems = SapSystem.query.filter_by(is_active=True).order_by(SapSystem.display_name).all()

    return render_template(
        "approval/queue.html",
        role=role,
        page=page,
        days_pending=days_pending,
        saved_views=saved_views,
        sap_systems=sap_systems,
        filters={"sap": sap_system_id, "min_amount": min_amount, "max_amount": max_amount},
        active_nav="approval_queue",
    )


@bp.route("/bg-multi-stage-approval/closure-verifications")
@login_required
def closure_verifications():
    if current_user.active_role != "abex":
        abort(403)
    closures = BgClosure.query.filter_by(stage="pending_abex_verification").order_by(
        BgClosure.id
    ).all()
    bg_map = {}
    for cl in closures:
        bg_map[cl.bank_guarantee_id] = db.session.get(BankGuarantee, cl.bank_guarantee_id)
    return render_template(
        "approval/closure_verifications.html",
        closures=closures, bg_map=bg_map, active_nav="approval_queue",
    )


@bp.route("/bg-multi-stage-approval/closure-verify/<int:closure_id>", methods=["POST"])
@login_required
@limiter.limit("30 per hour", methods=["POST"])
def closure_verify(closure_id):
    if current_user.active_role != "abex":
        abort(403)
    cl = db.session.get(BgClosure, closure_id)
    if not cl or cl.stage != "pending_abex_verification":
        abort(404)
    try:
        closure_service.verify_closure(cl, current_user)
    except PermissionError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("approval.closure_verifications"))
    flash("Closure verified and the Bank Guarantee closed.", "success")
    return redirect(url_for("approval.closure_verifications"))


@bp.route("/bg-multi-stage-approval/save-view", methods=["POST"])
@login_required
def save_view():
    if current_user.active_role not in _QUEUE_ROLES:
        abort(403)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Please name your saved view.", "danger")
        return redirect(url_for("approval.queue"))
    state = {
        "sap": request.form.get("sap", type=int),
        "min_amount": request.form.get("min_amount", type=float),
        "max_amount": request.form.get("max_amount", type=float),
    }
    sv = SavedView(
        user_id=current_user.id, page_key="approval_queue",
        name=name, filter_state=state,
    )
    db.session.add(sv)
    db.session.commit()
    audit_service.record(
        "queue_view_saved", actor_id=current_user.id, target_type="saved_view",
        target_id=sv.id, metadata_json={"page": "approval_queue", "name": name},
    )
    flash("View saved.", "success")
    return redirect(url_for("approval.queue"))


# ------------------------------------------------------------ review workspace

@bp.route("/bg-multi-stage-approval/<int:bg_id>")
@login_required
def review(bg_id):
    bg = db.session.get(BankGuarantee, bg_id)
    if not bg:
        abort(404)
    role = _require_stage_access(bg)
    visible = _visible(bg, role)
    prior_events, history = _prior_events(bg)
    analysis = intake_service.primary_analysis(bg)
    fields = (analysis.extracted_fields or {}) if analysis else {}
    po_result = intake_service.po_cross_check_result(bg)
    checklist = intake_service.format_checklist(bg)
    document = intake_service.primary_document(bg)
    is_abex = role == "abex"
    is_tc = role == "tc_head"
    rules = _prohibited_rules()
    stage_info = workflow_service.stage_info(bg)
    next_role = stage_info["next_role"]
    is_final = stage_info["is_final"]

    return render_template(
        "approval/review_workspace.html",
        bg=bg, role=role, visible=visible, prior_events=prior_events,
        history=history, fields=fields, po_result=po_result,
        checklist=checklist, document_id=document.id if document else None,
        is_abex=is_abex, is_tc=is_tc, rules=rules,
        requires_ceo=workflow_service.requires_ceo_cfo(bg),
        highest_tier=workflow_service.highest_tier(bg),
        dispatch_readiness=intake_service.dispatch_readiness(bg),
        stage_info=stage_info,
        next_role=next_role,
        is_final=is_final,
        active_nav="approval_queue",
    )


@bp.route("/bg-multi-stage-approval/<int:bg_id>/decide", methods=["POST"])
@login_required
@limiter.limit("60 per hour", methods=["POST"])
def decide(bg_id):
    bg = db.session.get(BankGuarantee, bg_id)
    if not bg:
        abort(404)
    role = _require_stage_access(bg)
    visible = _visible(bg, role)
    action = request.form.get("action")

    if action == "reject":
        overall = (request.form.get("overall_comment") or "").strip()
        if not overall:
            flash("Please provide an overall comment when rejecting the Bank Guarantee.", "danger")
            return redirect(url_for("approval.review", bg_id=bg.id))
        _record_decisions(bg, visible, role, save_forward=False)
        bg.status = BGStatus.rejected
        bg.current_stage = BGStatus.rejected.value
        bg.updated_at = datetime.utcnow()
        db.session.add(WorkflowHistory(
            bank_guarantee_id=bg.id, from_stage=_stage_before(bg), to_stage=BGStatus.rejected.value,
            action=WorkflowAction.reject, actor_id=current_user.id, actor_role=role,
            comments=overall,
        ))
        db.session.commit()
        audit_service.record(
            "bg_rejected", actor_id=current_user.id, target_type="bank_guarantee",
            target_id=bg.id, metadata_json={"bg_number": bg.bg_number, "role": role},
        )
        enqueue_fanout(bg, None, current_user.id, rejection=True)
        flash("The Bank Guarantee has been rejected.", "info")
        return redirect(url_for("approval.queue"))

    # approve / verify forward path
    if action not in ("forward", "verify"):
        abort(400)
    if role == "abex" and action != "verify":
        abort(400)

    # TC Head tier edits applied first (with deterministic severity floor).
    if role == "tc_head":
        _apply_tier_edits(bg, visible, role)

    # Server-side re-validation of every enablement condition.
    try:
        _validate_forward(bg, visible, role)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("approval.review", bg_id=bg.id))

    _record_decisions(bg, visible, role, save_forward=True)

    is_verify = role == "abex"
    next_role = None if is_verify else workflow_service.next_role(bg)
    is_final = is_verify or (next_role is None and workflow_service.is_final_stage(bg, role))

    if is_final:
        to_status = BGStatus.live
        is_verify = True
    elif next_role == "ceo_cfo":
        to_status = BGStatus.pending_ceo_cfo
    else:
        next_status = workflow_service.queue_role_status(next_role)
        if next_status is None:
            seq = workflow_service.full_sequence(bg)
            if not seq:
                flash("Workflow configuration error: No valid stage sequence configured in DoA matrix.", "danger")
            else:
                flash(f"Workflow configuration error: Could not determine next stage for role '{role}'.", "danger")
            return redirect(url_for("approval.review", bg_id=bg.id))
        to_status = BGStatus(next_status)

    from_stage = _stage_before(bg)
    bg.status = to_status
    bg.current_stage = to_status.value
    bg.updated_at = datetime.utcnow()
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage=from_stage, to_stage=to_status.value,
        action=WorkflowAction.verify if is_verify else WorkflowAction.approve_forward,
        actor_id=current_user.id, actor_role=role,
        comments=(request.form.get("overall_comment") or "").strip() or None,
    ))
    db.session.commit()
    audit_service.record(
        "bg_stage_advanced", actor_id=current_user.id, target_type="bank_guarantee",
        target_id=bg.id, metadata_json={"bg_number": bg.bg_number, "role": role,
                                        "from": from_stage, "to": to_status.value,
                                        "verify": is_verify},
    )
    enqueue_fanout(bg, next_role, current_user.id, to_live=is_verify)
    if is_verify:
        # Additive Step 6 trigger: bank-side authenticity verification on Live.
        from bgcc.services.bank_verification_service import trigger_verification

        try:
            trigger_verification(bg)
        except Exception:
            current_app.logger.exception("bank verification trigger failed for bg=%s", bg.id)
    flash("Bank Guarantee %s." % ("activated as Live" if is_verify else f"forwarded to {workflow_service.role_label(next_role)}"), "success")
    return redirect(url_for("approval.queue"))


def _stage_before(bg):
    return bg.current_stage or bg.status.value


def _validate_forward(bg, visible, role):
    decisions = {
        int(k.split("_", 1)[1]): v
        for k, v in request.form.items()
        if k.startswith("decision_")
    }
    for d in visible:
        # A prohibited-tier deviation with an admin override is treated as
        # resolved through its distinct override path - no decision needed.
        if d.effective_tier == "prohibited" and d.admin_override_by is not None:
            continue
        decision = decisions.get(d.id)
        if decision not in _DEV_DECISION:
            raise ValueError(f"Please record a decision for clause '{d.clause_reference}'.")
        if d.effective_tier == "prohibited" and decision == "accepted":
            raise ValueError(
                f"A prohibited-tier deviation ('{d.clause_reference}') can never be accepted. "
                "Only a platform administrator can clear this block."
            )
        if decision == "rejected":
            comment = (request.form.get(f"comment_{d.id}") or "").strip()
            if not comment:
                raise ValueError(f"A comment is required when rejecting clause '{d.clause_reference}'.")
        if d.is_missing_critical_clause:
            ack = request.form.get(f"ack_{d.id}")
            if ack not in ("on", "1", "true"):
                raise ValueError(f"Please acknowledge the missing critical clause '{d.clause_reference}'.")

    # A prohibited deviation only blocks forwarding when it has NO admin override.
    blocked = [
        d for d in visible
        if d.effective_tier == "prohibited" and d.admin_override_by is None
    ]
    if blocked:
        refs = ", ".join(d.clause_reference for d in blocked)
        raise ValueError(
            f"This Bank Guarantee carries a prohibited-tier deviation ({refs}) that cannot be "
            "forwarded. Only a platform administrator can clear this block."
        )

    if role == "abex":
        overall = (request.form.get("overall_comment") or "").strip()
        if not overall:
            raise ValueError("Please provide an overall comment before verifying and activating.")


def _record_decisions(bg, visible, role, save_forward):
    decisions = {
        int(k.split("_", 1)[1]): v
        for k, v in request.form.items()
        if k.startswith("decision_")
    }
    for d in visible:
        # Overridden prohibited deviations are resolved via their override path,
        # never through the ordinary Accept/Reject mechanism.
        if d.effective_tier == "prohibited" and d.admin_override_by is not None:
            continue
        decision = decisions.get(d.id)
        if decision not in _DEV_DECISION:
            if save_forward:
                continue
            continue
        comment = (request.form.get(f"comment_{d.id}") or "").strip()
        d.status = "accepted" if decision == "accepted" else "rejected"
        d.decided_by = current_user.id
        d.decided_at = datetime.utcnow()
        d.decision_comment = comment or None
        audit_service.record(
            "deviation_decision", actor_id=current_user.id, target_type="bank_guarantee",
            target_id=bg.id, metadata_json={
                "deviation_id": d.id, "clause_reference": d.clause_reference,
                "decision": d.status, "role": role, "comment": comment,
            },
        )
    db.session.commit()


def _apply_tier_edits(bg, visible, role):
    rules = _prohibited_rules()
    for d in visible:
        raw = request.form.get(f"tier_{d.id}")
        if raw not in ("low", "high", "prohibited"):
            continue
        # Severity floor: effective is the more severe of TC selection and the
        # deterministic prohibited-pattern verdict for this clause's text.
        new_eff, matched = effective_tier(raw, d.bg_text_excerpt, rules)
        if new_eff != d.effective_tier:
            d.effective_tier = new_eff
            d.ai_proposed_tier = raw  # record the TC Head's authoritative proposal
            d.tier_changed_by = current_user.id
            if matched:
                # Deterministic verdict forced a higher tier than the TC selection.
                pass
            audit_service.record(
                "deviation_tier_changed", actor_id=current_user.id, target_type="bank_guarantee",
                target_id=bg.id, metadata_json={
                    "deviation_id": d.id, "clause_reference": d.clause_reference,
                    "selected_tier": raw, "effective_tier": new_eff,
                    "rule_forced": bool(matched), "role": role,
                },
            )
    db.session.commit()


def _prohibited_rules():
    from bgcc.models.settings import ApplicationSetting

    setting = ApplicationSetting.query.filter_by(setting_key="prohibited_clause_patterns").first()
    value = setting.setting_value if setting else []
    if isinstance(value, dict):
        value = value.get("rules") or []
    return value or []


# ----------------------------------------------------------- CEO/CFO mail

@bp.route("/bg-ceo-cfo-mail", methods=["GET", "POST"])
@login_required
@limiter.limit("20 per hour", methods=["POST"])
def ceo_cfo_mail():
    if current_user.active_role != PlatformRole.creator.value and current_user.active_role != "creator":
        abort(403)
    if request.method == "POST":
        bg_id = request.form.get("bg_id", type=int)
        bg = db.session.get(BankGuarantee, bg_id)
        if not bg or bg.status not in (BGStatus.pending_ceo_cfo, BGStatus.pending_ceo_cfo.value) or bg.creator_id != current_user.id:
            abort(403)
        confirm = request.form.get("confirm")
        if confirm not in ("on", "1", "true"):
            flash("Please confirm this represents genuine CEO/CFO sign-off received via email.", "danger")
            return redirect(url_for("approval.ceo_cfo_mail"))
        file_storage = request.files.get("file")
        if not file_storage or not file_storage.filename:
            flash("Please attach the CEO/CFO approval-evidence document.", "danger")
            return redirect(url_for("approval.ceo_cfo_mail"))
        if not intake_service.is_pdf(file_storage):
            flash("Only PDF evidence files are accepted.", "danger")
            return redirect(url_for("approval.ceo_cfo_mail"))
        upload_root = current_app.config["UPLOAD_FOLDER"]
        path, original, size = intake_service.save_uploaded_file(file_storage, upload_root)
        db.session.add(Document(
            bank_guarantee_id=bg.id, document_type="offline_approval",
            storage_path=path, original_filename=original, mime_type="application/pdf",
            file_size_bytes=size, uploaded_by=current_user.id,
        ))
        ref = (request.form.get("reference") or "").strip()
        bg.status = BGStatus.pending_abex_verification
        bg.current_stage = BGStatus.pending_abex_verification.value
        bg.updated_at = datetime.utcnow()
        db.session.add(WorkflowHistory(
            bank_guarantee_id=bg.id, from_stage=BGStatus.pending_ceo_cfo.value,
            to_stage=BGStatus.pending_abex_verification.value,
            action=WorkflowAction.approve_forward, actor_id=current_user.id,
            actor_role=current_user.active_role,
            comments=("CEO/CFO email evidence attached" + (f" ({ref})" if ref else "")),
        ))
        db.session.commit()
        audit_service.record(
            "ceo_cfo_evidence_attached", actor_id=current_user.id, target_type="bank_guarantee",
            target_id=bg.id, metadata_json={"bg_number": bg.bg_number, "reference": ref},
        )
        enqueue_fanout(bg, PlatformRole.abex.value, current_user.id)
        flash("CEO/CFO evidence attached and the Bank Guarantee advanced to ABEX verification.", "success")
        return redirect(url_for("approval.ceo_cfo_mail"))

    bgs = BankGuarantee.query.filter(
        db.or_(
            BankGuarantee.status == BGStatus.pending_ceo_cfo,
            BankGuarantee.status == BGStatus.pending_ceo_cfo.value,
        ),
        BankGuarantee.creator_id == current_user.id,
    ).order_by(BankGuarantee.updated_at.desc()).all()
    return render_template("approval/ceo_cfo_mail.html", bgs=bgs, active_nav="ceo_cfo_mail")
