from datetime import date, datetime

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

from bgcc.content import AI_DISCLAIMER
from bgcc.extensions import db, limiter
from bgcc.models.documents import Document
from bgcc.models.enums import BGStatus, WorkflowAction
from bgcc.models.lifecycle import BgClosure, BgReturn, ExtensionRequest
from bgcc.models.reference import BankGuarantee
from bgcc.models.users import User
from bgcc.models.workflow import WorkflowHistory
from bgcc.services import (
    audit_service,
    closure_service,
    extension_service,
    intake_service,
    invocation_service,
    magic_link_service,
)

bp = Blueprint("lifecycle", __name__, url_prefix="")


def _coordinator_only():
    if current_user.active_role != "coordinator":
        abort(403)


def _live_bg(bg_id):
    bg = db.session.get(BankGuarantee, int(bg_id))
    if not bg or bg.status != BGStatus.live.value:
        abort(404)
    return bg


# ------------------------------------------------------------- BG extension

@bp.route("/bg-extension", methods=["GET", "POST"])
@login_required
def extension():
    _coordinator_only()
    if request.method == "POST":
        bg_id = request.form.get("bg_id", type=int)
        bg = db.session.get(BankGuarantee, bg_id)
        if not bg or bg.status != BGStatus.live.value:
            abort(404)
        try:
            extension_service.initiate_extension(
                bg, current_user,
                vendor_email=request.form.get("vendor_email", ""),
                message=request.form.get("message", ""),
            )
            flash("Extension request sent to the vendor.", "success")
        except ValueError as exc:
            flash(str(exc), "danger")
        return redirect(url_for("lifecycle.extension"))

    live_bgs = BankGuarantee.query.filter_by(status=BGStatus.live.value).all()
    reqs = ExtensionRequest.query.order_by(ExtensionRequest.id.desc()).all()
    req_by_bg = {r.parent_bg_id: r for r in reqs}
    bg_by_id = {b.id: b for b in live_bgs}

    today = date.today()
    sections = {"approaching": [], "requested": [], "overdue": [], "completed": []}
    for bg in live_bgs:
        req = req_by_bg.get(bg.id)
        days = (bg.expiry_date - today).days
        row = {
            "bg": bg,
            "req": req,
            "days": days,
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

    sections["approaching"].sort(key=lambda r: r["days"])
    sections["requested"].sort(key=lambda r: r["days"])
    sections["overdue"].sort(key=lambda r: r["days"])
    sections["completed"].sort(key=lambda r: r["days"])
    return render_template("lifecycle/extension.html", sections=sections,
                           active_nav="extension")


@bp.route("/bg-extension/upload-link/<int:parent_id>")
@login_required
def extension_upload_link(parent_id):
    _coordinator_only()
    return redirect(url_for("intake.upload_extended", parent=parent_id))


# ------------------------------------------------------------- BG closure

@bp.route("/bg-closure", methods=["GET", "POST"])
@login_required
@limiter.limit("20 per hour", methods=["POST"])
def closure():
    _coordinator_only()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "initiate":
            bg = _live_bg(request.form.get("bg_id"))
            try:
                closure_service.initiate_closure(
                    bg, current_user,
                    justification=request.form.get("justification", ""),
                )
                if closure_service.active_closure_for(bg.id).is_exception:
                    flash("Exception closure initiated - routed for category-lead review.", "success")
                else:
                    flash("Standard closure initiated - routed to ABEX verification.", "success")
            except ValueError as exc:
                flash(str(exc), "danger")
            return redirect(url_for("lifecycle.closure"))
        elif action == "offline_attachments":
            closure_id = request.form.get("closure_id", type=int)
            cl = db.session.get(BgClosure, closure_id)
            if not cl or cl.initiated_by != current_user.id:
                abort(403)
            if cl.stage not in ("pending_cfo", "pending_ceo"):
                flash("Offline evidence can only be attached at the CFO or CEO stage.", "danger")
                return redirect(url_for("lifecycle.closure"))
            file_storage = request.files.get("file")
            if not file_storage or not file_storage.filename:
                flash("Please attach the combined CFO-and-CEO sign-off evidence.", "danger")
                return redirect(url_for("lifecycle.closure"))
            if not intake_service.is_pdf(file_storage):
                flash("Only PDF evidence files are accepted.", "danger")
                return redirect(url_for("lifecycle.closure"))
            bg = db.session.get(BankGuarantee, cl.bank_guarantee_id)
            upload_root = current_app.config["UPLOAD_FOLDER"]
            path, original, size = intake_service.save_uploaded_file(file_storage, upload_root)
            db.session.add(Document(
                bank_guarantee_id=bg.id, document_type="offline_approval",
                storage_path=path, original_filename=original, mime_type="application/pdf",
                file_size_bytes=size, uploaded_by=current_user.id,
            ))
            cl.stage = "pending_abex_verification"
            db.session.add(WorkflowHistory(
                bank_guarantee_id=bg.id, from_stage="pending_approval_attachments",
                to_stage="pending_abex_verification",
                action=WorkflowAction.closure_reviewed, actor_id=current_user.id,
                actor_role="coordinator",
                comments="Combined offline CFO/CEO sign-off evidence attached.",
            ))
            db.session.commit()
            audit_service.record(
                "closure_offline_attachments", actor_id=current_user.id,
                target_type="bg_closure", target_id=cl.id,
                metadata_json={"bg_number": bg.bg_number},
            )
            flash("Offline CFO/CEO evidence attached - closure routed to ABEX verification.", "success")
            return redirect(url_for("lifecycle.closure"))

    live_bgs = BankGuarantee.query.filter_by(status=BGStatus.live.value).all()
    existing = {c.bank_guarantee_id for c in BgClosure.query.filter(
        BgClosure.stage != closure_service.CLOSURE_STAGE_TERMINAL
    ).all()}
    closable = [b for b in live_bgs if b.id not in existing]
    my_closures = BgClosure.query.filter_by(initiated_by=current_user.id).order_by(
        BgClosure.id.desc()
    ).all()
    bg_map = {b.id: b for b in live_bgs}
    return render_template("lifecycle/closure.html", closable=closable,
                           my_closures=my_closures, bg_map=bg_map,
                           active_nav="closure")


# ------------------------------------------------------ Closure review (TC Head)

@bp.route("/bg-closure-category-lead", methods=["GET", "POST"])
@login_required
@limiter.limit("30 per hour", methods=["POST"])
def closure_review():
    if current_user.active_role != "tc_head":
        abort(403)
    if request.method == "POST":
        closure_id = request.form.get("closure_id", type=int)
        cl = db.session.get(BgClosure, closure_id)
        if not cl or cl.stage != "pending_category_lead":
            abort(404)
        comment = (request.form.get("comment") or "").strip()
        if not comment:
            flash("A comment is required.", "danger")
            return redirect(url_for("lifecycle.closure_review"))
        bg = db.session.get(BankGuarantee, cl.bank_guarantee_id)
        decision = request.form.get("decision")
        if decision == "approve":
            cl.stage = "pending_cfo"
            db.session.add(WorkflowHistory(
                bank_guarantee_id=bg.id, from_stage="pending_category_lead",
                to_stage="pending_cfo", action=WorkflowAction.closure_reviewed,
                actor_id=current_user.id, actor_role="tc_head", comments=comment,
            ))
            db.session.commit()
            audit_service.record(
                "closure_reviewed", actor_id=current_user.id, target_type="bg_closure",
                target_id=cl.id, metadata_json={"bg_number": bg.bg_number, "decision": "approve"},
            )
            # Dispatch the first (CFO) magic-link email now.
            try:
                closure_service.dispatch_cfo_approval(cl)
            except ValueError as exc:
                flash(f"Closure approved, but CFO email could not be sent: {exc}", "warning")
            flash("Closure approved - CFO approval email dispatched.", "success")
        elif decision == "reject":
            cl.stage = closure_service.CLOSURE_STAGE_TERMINAL
            db.session.add(WorkflowHistory(
                bank_guarantee_id=bg.id, from_stage="pending_category_lead",
                to_stage=closure_service.CLOSURE_STAGE_TERMINAL,
                action=WorkflowAction.closure_rejected, actor_id=current_user.id,
                actor_role="tc_head", comments=comment,
            ))
            db.session.commit()
            audit_service.record(
                "closure_reviewed", actor_id=current_user.id, target_type="bg_closure",
                target_id=cl.id, metadata_json={"bg_number": bg.bg_number, "decision": "reject"},
            )
            flash("Closure rejected.", "info")
        return redirect(url_for("lifecycle.closure_review"))

    closures = BgClosure.query.filter_by(stage="pending_category_lead").order_by(
        BgClosure.id
    ).all()
    bg_map = {}
    for cl in closures:
        bg_map[cl.bank_guarantee_id] = db.session.get(BankGuarantee, cl.bank_guarantee_id)
    return render_template("lifecycle/closure_review.html", closures=closures,
                           bg_map=bg_map, active_nav="closure_review")


# ------------------------------------------------------------- BG return

def _return_eligible_bgs():
    query = BankGuarantee.query.filter(
        BankGuarantee.status.in_([BGStatus.live.value, BGStatus.closed.value])
    )
    if current_user.active_role == "coordinator":
        return query.all()
    # Limited creator access: BGs the user originally submitted.
    if current_user.active_role == "creator":
        return query.filter(BankGuarantee.creator_id == current_user.id).all()
    abort(403)


@bp.route("/bg-return", methods=["GET", "POST"])
@login_required
@limiter.limit("20 per hour", methods=["POST"])
def bg_return():
    if current_user.active_role not in ("coordinator", "creator"):
        abort(403)
    if request.method == "POST":
        action = request.form.get("action")
        bg = db.session.get(BankGuarantee, request.form.get("bg_id", type=int))
        if not bg:
            abort(404)
        if current_user.active_role == "creator" and bg.creator_id != current_user.id:
            abort(403)

        if action == "request":
            existing = BgReturn.query.filter_by(bank_guarantee_id=bg.id).first()
            if existing and existing.status != "receipt_confirmed":
                flash("A return request already exists for this BG.", "danger")
                return redirect(url_for("lifecycle.bg_return"))
            ret = BgReturn(bank_guarantee_id=bg.id, status="requested",
                           requested_by=current_user.id)
            db.session.add(ret)
            db.session.flush()
            db.session.add(WorkflowHistory(
                bank_guarantee_id=bg.id, from_stage=bg.status.value,
                to_stage="return_requested", action=WorkflowAction.return_requested,
                actor_id=current_user.id, actor_role=current_user.active_role,
            ))
            db.session.commit()
            audit_service.record(
                "return_requested", actor_id=current_user.id, target_type="bg_return",
                target_id=ret.id, metadata_json={"bg_number": bg.bg_number},
            )
            flash("Return request raised.", "success")
        elif action == "dispatch":
            ret = _own_return(bg)
            if not ret or ret.status != "requested":
                abort(404)
            mode = request.form.get("dispatch_mode")
            if mode not in ("courier", "cmr"):
                flash("Please choose a dispatch mode (courier or CMR).", "danger")
                return redirect(url_for("lifecycle.bg_return"))
            if mode == "courier" and not (request.form.get("courier_name") and request.form.get("tracking_number")):
                flash("Courier name and tracking number are required.", "danger")
                return redirect(url_for("lifecycle.bg_return"))
            if mode == "cmr" and not (request.form.get("cmr_deliverer_name") and request.form.get("cmr_deliverer_mobile")):
                flash("CMR deliverer name and mobile are required.", "danger")
                return redirect(url_for("lifecycle.bg_return"))
            from bgcc.models.dispatches import Dispatch

            dispatch = Dispatch(
                bank_guarantee_id=bg.id, context_type="return", dispatch_mode=mode,
                courier_name=request.form.get("courier_name") if mode == "courier" else None,
                tracking_number=request.form.get("tracking_number") if mode == "courier" else None,
                cmr_deliverer_name=request.form.get("cmr_deliverer_name") if mode == "cmr" else None,
                cmr_deliverer_email=request.form.get("cmr_deliverer_email") or None,
                cmr_deliverer_mobile=request.form.get("cmr_deliverer_mobile") if mode == "cmr" else None,
                dispatched_by=current_user.id,
            )
            db.session.add(dispatch)
            db.session.flush()
            ret.dispatch_id = dispatch.id
            ret.status = "dispatched"
            db.session.add(WorkflowHistory(
                bank_guarantee_id=bg.id, from_stage="return_requested",
                to_stage="return_dispatched", action=WorkflowAction.return_dispatched,
                actor_id=current_user.id, actor_role=current_user.active_role,
            ))
            db.session.commit()
            audit_service.record(
                "return_dispatched", actor_id=current_user.id, target_type="bg_return",
                target_id=ret.id, metadata_json={"bg_number": bg.bg_number, "mode": mode},
            )
            flash("Physical return marked as dispatched.", "success")
        elif action == "confirm_receipt":
            ret = _own_return(bg)
            if not ret or ret.status != "dispatched":
                abort(404)
            ret.status = "receipt_confirmed"
            ret.receipt_confirmed_by = current_user.id
            ret.receipt_confirmed_at = datetime.utcnow()
            db.session.add(WorkflowHistory(
                bank_guarantee_id=bg.id, from_stage="return_dispatched",
                to_stage="return_receipt_confirmed",
                action=WorkflowAction.return_receipt_confirmed,
                actor_id=current_user.id, actor_role=current_user.active_role,
                comments=request.form.get("note") or "",
            ))
            db.session.commit()
            audit_service.record(
                "return_receipt_confirmed", actor_id=current_user.id,
                target_type="bg_return", target_id=ret.id,
                metadata_json={"bg_number": bg.bg_number},
            )
            flash("Receipt confirmed.", "success")
        return redirect(url_for("lifecycle.bg_return"))

    eligible = _return_eligible_bgs()
    returns = BgReturn.query.order_by(BgReturn.id.desc()).all()
    ret_by_bg = {r.bank_guarantee_id: r for r in returns}
    bg_map = {b.id: b for b in eligible}
    return render_template("lifecycle/return.html", eligible=eligible,
                           ret_by_bg=ret_by_bg, bg_map=bg_map,
                           active_nav="bg_return")


def _own_return(bg):
    ret = BgReturn.query.filter_by(bank_guarantee_id=bg.id).first()
    if not ret:
        return None
    if current_user.active_role == "creator" and bg.creator_id != current_user.id:
        return None
    return ret


# -------------------------------------------------------- executive magic link

@bp.route("/executive-approval", methods=["GET", "POST"])
@bp.route("/executive-approval/<token>", methods=["GET", "POST"])
@bp.route("/lifecycle/executive-approval/<token>", methods=["GET", "POST"])
@bp.route("/lifecycle/executive-approval", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def executive_approval(token=None):
    token = token or request.args.get("token") or request.form.get("token")
    if not token:
        return render_template("lifecycle/executive_invalid.html", token=""), 400

    key, record = magic_link_service.resolve_token(token)
    if key is None or record is None or not magic_link_service.is_usable(key, record):
        return render_template("lifecycle/executive_invalid.html", token=token), 400

    ctx = magic_link_service.token_context(key, record)
    if request.method == "POST":
        if request.form.get("action") == "approve":
            try:
                magic_link_service.consume_and_apply(key, record)
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("lifecycle.executive_approval", token=token))
            flash("Approval recorded. Thank you.", "success")
            return render_template("lifecycle/executive_done.html", approved=True)
        elif request.form.get("action") == "decline":
            magic_link_service.consume_and_decline(key, record)
            return render_template("lifecycle/executive_done.html", approved=False)

    # Build display context for closure, invocation, or bank-verification approvals.
    bg = db.session.get(BankGuarantee, record.bank_guarantee_id)
    requested_user = None
    if key.startswith("closure_") and getattr(record, "initiated_by", None):
        requested_user = db.session.get(User, record.initiated_by)
    elif key.startswith("invocation_") and getattr(record, "hold_requested_by", None):
        requested_user = db.session.get(User, record.hold_requested_by)

    return render_template(
        "lifecycle/executive_approval.html",
        token=token,
        ctx=ctx,
        closure=record if key.startswith("closure_") else None,
        invocation=record if key.startswith("invocation_") else None,
        bank_verification=record if key == "bank_verification" else None,
        bg=bg,
        role=_executive_role(key),
        requested_by=requested_user,
    )


def _executive_role(key):
    if key == "closure_cfo":
        return "CFO"
    if key == "closure_ceo":
        return "CEO"
    if key == "invocation_ceo":
        return "CEO"
    if key == "invocation_hold_cfo":
        return "CFO"
    if key == "invocation_hold_ceo":
        return "CEO"
    return ""
