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
from bgcc.models.documents import Document
from bgcc.models.enums import BGStatus, JobStatus, WorkflowAction
from bgcc.models.generated_documents import GeneratedDocument
from bgcc.models.jobs import CeleryJob
from bgcc.models.lifecycle import BgInvocation
from bgcc.models.reference import BankGuarantee
from bgcc.models.workflow import WorkflowHistory
from bgcc.services import (
    access_service,
    audit_service,
    intake_service,
    invocation_service,
    workflow_service,
)

import logging

logger = logging.getLogger(__name__)

bp = Blueprint("invocation", __name__, url_prefix="")


def _require(role):
    user_roles = set(current_user.granted_roles or []) if current_user else set()
    if current_user.active_role != role and role not in user_roles and "admin" not in user_roles:
        abort(403)


def _live_bg(bg_id):
    bg = db.session.get(BankGuarantee, int(bg_id))
    if not bg:
        abort(404)
    st = bg.status.value if hasattr(bg.status, "value") else str(bg.status)
    stage = str(bg.current_stage or "")
    if st not in (BGStatus.live.value, "live", "approved") and stage not in ("live", "approved"):
        abort(404)
    if not access_service.can_view_bg(current_user, bg):
        abort(403)
    return bg


def _own_invocation(bg):
    return invocation_service.latest_invocation(bg)


def _enqueue(job):
    job = CeleryJob(task_name=job, status=JobStatus.queued,
                    related_bg_id=None, triggered_by=current_user.id)
    db.session.add(job)
    db.session.commit()
    return job


@bp.route("/bg-invocation")
@login_required
def index():
    user_roles = set(current_user.granted_roles or []) if current_user else set()
    active_role = getattr(current_user, "active_role", None)
    allowed_roles = {"bu_fc", "tc_head", "admin"}
    if active_role not in allowed_roles and not (user_roles & allowed_roles):
        abort(403)

    effective_role = active_role if active_role in ("bu_fc", "tc_head") else (
        "bu_fc" if "bu_fc" in user_roles else ("tc_head" if "tc_head" in user_roles else "bu_fc")
    )

    live_bgs = invocation_service.get_live_bgs_for_user(current_user)
    inv_map = {b.id: invocation_service.latest_invocation(b) for b in live_bgs}

    monitor = []
    in_progress = []
    completed = []

    for bg in live_bgs:
        inv = inv_map.get(bg.id)
        window = invocation_service.evaluate_claim_window(bg)

        if inv and (inv.stage == "sent_to_bank" or inv.sent_to_bank_at is not None):
            completed.append({"bg": bg, "inv": inv, "window": window})
        elif inv and inv.stage in ("draft_generated", "signed_uploaded", "on_hold", "ceo_declined"):
            in_progress.append({"bg": bg, "inv": inv, "window": window})
        else:
            monitor.append({"bg": bg, "inv": inv, "window": window})

    monitor.sort(key=lambda x: (
        0 if x["window"]["is_critical"] else (1 if x["window"]["is_in_window"] else 2),
        x["bg"].expiry_date
    ))
    in_progress.sort(key=lambda x: x["bg"].expiry_date)
    completed.sort(key=lambda x: (x["inv"].sent_to_bank_at or datetime.min) if x.get("inv") else datetime.min, reverse=True)

    logger.info(
        "Invocation page rendered for user %s (role=%s): %s in monitor, %s in_progress, %s completed",
        current_user.email, effective_role, len(monitor), len(in_progress), len(completed)
    )

    def doc_refs(inv):
        docs = {"signed": None, "docx": None, "pdf": None}
        if not inv:
            return docs
        if inv.signed_document_id:
            docs["signed"] = db.session.get(Document, inv.signed_document_id)
        gens = GeneratedDocument.query.filter_by(
            bank_guarantee_id=inv.bank_guarantee_id,
            document_kind=invocation_service.INVOCATION_DOC_KIND,
        ).order_by(GeneratedDocument.version.desc()).all()
        for g in gens:
            if g.file_format == "docx" and docs["docx"] is None:
                docs["docx"] = g
            if g.file_format == "pdf" and docs["pdf"] is None:
                docs["pdf"] = g
        return docs

    claim_days = {b.id: invocation_service.claim_window_days(b) for b in live_bgs}
    bank_emails = {b.id: invocation_service._bank_contact_email(b) for b in live_bgs}
    return render_template(
        "invocation/invocation.html",
        monitor=monitor, in_progress=in_progress, completed=completed,
        doc_refs=doc_refs, role=effective_role,
        claim_days=claim_days, bank_emails=bank_emails,
        active_nav="invocation",
    )


@bp.route("/bg-invocation/generate", methods=["POST"])
@login_required
@limiter.limit("10 per hour", methods=["POST"])
def generate():
    _require("bu_fc")
    bg = _live_bg(request.form.get("bg_id"))
    inv = invocation_service.get_or_create_invocation(bg)
    # Regenerate is allowed while draft_generated and no signed letter yet.
    if inv.signed_document_id:
        flash("The draft cannot be regenerated because a signed letter has already been uploaded.", "danger")
        return redirect(url_for("invocation.index"))
    job = CeleryJob(task_name="invocation.generate_draft", status=JobStatus.queued,
                    related_bg_id=bg.id, triggered_by=current_user.id)
    db.session.add(job)
    db.session.commit()
    from bgcc.tasks.invocation_tasks import generate_draft as task

    result = task.apply_async(args=[bg.id, current_user.id, job.id])
    if result and getattr(result, "id", None):
        job.celery_task_id = result.id
        db.session.commit()
    flash("Invocation draft generation started. It may take a moment.", "info")
    return redirect(url_for("invocation.index"))


@bp.route("/bg-invocation/sign-upload", methods=["POST"])
@login_required
@limiter.limit("20 per hour", methods=["POST"])
def sign_upload():
    _require("bu_fc")
    bg = _live_bg(request.form.get("bg_id"))
    inv = _own_invocation(bg)
    if not inv:
        abort(404)
    confirm = request.form.get("signed_confirm")
    if confirm not in ("on", "1", "true"):
        flash("Please confirm the letter has been signed before uploading it.", "danger")
        return redirect(url_for("invocation.index"))
    file_storage = request.files.get("file")
    if not file_storage or not file_storage.filename:
        flash("Please attach the signed invocation letter (PDF).", "danger")
        return redirect(url_for("invocation.index"))
    if not intake_service.is_pdf(file_storage):
        flash("Only PDF files are accepted for the signed letter.", "danger")
        return redirect(url_for("invocation.index"))
    file_storage.stream.seek(0, 2)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    max_bytes = current_app.config["EXTENDED_BG_MAX_MB"] * 1024 * 1024
    if size > max_bytes:
        flash(f"The signed letter exceeds the {current_app.config['EXTENDED_BG_MAX_MB']} MB upload limit.", "danger")
        return redirect(url_for("invocation.index"))

    upload_root = current_app.config["UPLOAD_FOLDER"]
    path, original, _ = intake_service.save_uploaded_file(file_storage, upload_root)
    doc = Document(
        bank_guarantee_id=bg.id, document_type="signed_invocation_letter",
        storage_path=path, original_filename=original, mime_type="application/pdf",
        file_size_bytes=size, uploaded_by=current_user.id,
    )
    db.session.add(doc)
    db.session.flush()
    inv.signed_document_id = doc.id
    if inv.stage == "draft_generated":
        inv.stage = "signed_uploaded"
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="draft_generated", to_stage="signed_uploaded",
        action=WorkflowAction.invocation_signed_uploaded, actor_id=current_user.id,
        actor_role="bu_fc", comments="Signed invocation letter uploaded.",
    ))
    db.session.commit()
    audit_service.record(
        "invocation_signed_uploaded", actor_id=current_user.id, target_type="bg_invocation",
        target_id=inv.id, metadata_json={"bg_number": bg.bg_number, "document_id": doc.id},
    )
    # Run the send-gate evaluator.
    invocation_service.evaluate_and_send(inv)
    flash("Signed letter uploaded.", "success")
    return redirect(url_for("invocation.index"))


@bp.route("/bg-invocation/hold", methods=["POST"])
@login_required
@limiter.limit("20 per hour", methods=["POST"])
def hold():
    _require("tc_head")
    bg = _live_bg(request.form.get("bg_id"))
    inv = _own_invocation(bg)
    if not inv:
        abort(404)
    if inv.sent_to_bank_at is not None:
        flash("This invocation has already been sent to the bank and cannot be held.", "danger")
        return redirect(url_for("invocation.index"))
    invocation_service.request_hold(inv, current_user)
    flash("Hold requested. The send path is immediately suspended pending CFO and CEO approval.", "info")
    return redirect(url_for("invocation.index"))


@bp.route("/bg-invocation/release", methods=["POST"])
@login_required
@limiter.limit("20 per hour", methods=["POST"])
def release():
    _require("tc_head")
    bg = _live_bg(request.form.get("bg_id"))
    inv = _own_invocation(bg)
    if not inv or inv.stage != "on_hold":
        abort(404)
    invocation_service.release_hold(inv, user=current_user, comment="TC Head released the hold.")
    flash("Hold released.", "success")
    return redirect(url_for("invocation.index"))
