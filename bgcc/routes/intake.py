from datetime import date, datetime, timedelta

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
from bgcc.forms import ExtendedBgDetailsForm, IntakeReviewForm, NewBgDetailsForm
from bgcc.models.deviations import Deviation
from bgcc.models.documents import Document, DocumentAnalysis
from bgcc.models.enums import BGStatus, JobStatus
from bgcc.models.jobs import CeleryJob
from bgcc.models.reference import BankGuarantee, SapSystem
from bgcc.services import audit_service, intake_service, sap_service

bp = Blueprint("intake", __name__, url_prefix="")


def _sap_system_choices():
    return [
        (s.id, f"{s.display_name} ({s.code})")
        for s in SapSystem.query.filter_by(is_active=True).order_by(SapSystem.display_name).all()
    ]


def _load_owned_bg(bg_id, expected_role):
    bg = db.session.get(BankGuarantee, int(bg_id))
    if not bg:
        abort(404)
    is_extension = bool(bg.parent_bg_id)
    owner_id = bg.coordinator_id if is_extension else bg.creator_id
    if current_user.active_role != expected_role or owner_id != current_user.id:
        abort(403)
    return bg, is_extension


def _pipeline_ready(bg_id):
    return intake_service.get_pipeline_status(db.session.get(BankGuarantee, bg_id))["overall"] == "ready"


def _po_numbers():
    return [p.strip() for p in request.form.getlist("po_number") if p and p.strip()]


def _validate_upload_file(file_storage, max_bytes):
    if not file_storage or not file_storage.filename:
        return "Please attach the Bank Guarantee PDF."
    if not intake_service.is_pdf(file_storage):
        return "Only PDF files are accepted. Please upload a PDF (digital or scanned)."
    file_storage.stream.seek(0, 2)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > max_bytes:
        return f"The file exceeds the {max_bytes // (1024*1024)} MB upload limit."
    return None


def _enqueue_stage(bg_id, user_id, task_name):
    job = CeleryJob(
        task_name=task_name,
        status=JobStatus.queued,
        related_bg_id=bg_id,
        triggered_by=user_id,
    )
    db.session.add(job)
    db.session.commit()
    from bgcc.tasks.ai_tasks import bg_extraction

    task = bg_extraction.apply_async(args=[bg_id, user_id, job.id])
    if task.id:
        job.celery_task_id = task.id
        db.session.commit()
    return job


def _common_po_validation(po_numbers):
    try:
        po_context = sap_service.get_po_context(po_numbers)
    except ValueError as exc:
        return None, str(exc)
    same_vendor_err = intake_service.validate_po_same_vendor(po_context)
    if same_vendor_err:
        return None, same_vendor_err
    return po_context, None


def _save_and_create(user, *, sap_system_id, bg_type, format_variant,
                     expenditure_type, po_numbers, po_context, document_type,
                     parent_bg_id=None, provisional_issue=None, provisional_expiry=None):
    file_storage = request.files.get("file")
    upload_root = current_app.config["UPLOAD_FOLDER"]
    path, original, size = intake_service.save_uploaded_file(file_storage, upload_root)
    bg = intake_service.create_draft_bg(
        user,
        sap_system_id=sap_system_id,
        bg_type=bg_type,
        format_variant=format_variant,
        expenditure_type=expenditure_type,
        po_numbers=po_numbers,
        parent_bg_id=parent_bg_id,
        provisional_vendor=po_context[0]["vendor_name"] if po_context else None,
    )
    if provisional_issue:
        bg.issue_date = provisional_issue
        bg.expiry_date = provisional_expiry or provisional_issue
    intake_service.create_document(bg.id, user.id, path, original, size, document_type)
    db.session.commit()
    return bg


# ---------------------------------------------------------------- New BG intake

@bp.route("/bg-upload", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per hour", methods=["POST"])
def upload_bg():
    if current_user.active_role != "creator":
        abort(403)
    form = NewBgDetailsForm()
    form.sap_system_id.choices = _sap_system_choices()
    extra_errors = {}
    if request.method == "POST" and form.validate_on_submit():
        po_numbers = _po_numbers()
        if not po_numbers:
            extra_errors["po_number"] = "Enter at least one PO number."
        else:
            file_err = _validate_upload_file(request.files.get("file"),
                                             intake_service.new_bg_max_bytes())
            if file_err:
                extra_errors["file"] = file_err
            else:
                po_context, po_err = _common_po_validation(po_numbers)
                if po_err:
                    extra_errors["po_number"] = po_err
                else:
                    bg = _save_and_create(
                        current_user,
                        sap_system_id=form.sap_system_id.data,
                        bg_type=form.bg_type.data,
                        format_variant=form.format_variant.data,
                        expenditure_type=form.expenditure_type.data,
                        po_numbers=po_numbers,
                        po_context=po_context,
                        document_type="original_bg",
                    )
                    audit_service.record(
                        "bg_intake_started",
                        actor_id=current_user.id,
                        target_type="bank_guarantee",
                        target_id=bg.id,
                        metadata_json={"bg_number": bg.bg_number, "po_numbers": po_numbers},
                    )
                    _enqueue_stage(bg.id, current_user.id, "bg_extraction")
                    return redirect(url_for("intake.upload_bg_progress", bg_id=bg.id))
    return render_template(
        "intake/upload_bg.html", form=form, active_nav="upload_bg",
        max_mb=current_app.config["NEW_BG_MAX_MB"], extra_errors=extra_errors,
    )


@bp.route("/bg-upload/<int:bg_id>/progress")
@login_required
def upload_bg_progress(bg_id):
    bg, _ = _load_owned_bg(bg_id, "creator")
    return render_template("intake/progress.html", bg=bg, bg_id=bg_id,
                           is_extension=False, active_nav="upload_bg")


@bp.route("/bg-upload/<int:bg_id>/review")
@login_required
def upload_bg_review(bg_id):
    return _render_review(bg_id, is_extension=False)


@bp.route("/bg-upload/<int:bg_id>/save-draft", methods=["POST"])
@login_required
def save_bg_draft(bg_id):
    return _save_draft(bg_id, is_extension=False)


@bp.route("/bg-upload/<int:bg_id>/submit", methods=["POST"])
@login_required
def submit_bg(bg_id):
    return _submit(bg_id, is_extension=False)


@bp.route("/bg-upload/<int:bg_id>/discard", methods=["POST"])
@login_required
def discard_bg(bg_id):
    return _discard(bg_id, is_extension=False)


# ---------------------------------------------------------- Extended BG intake

@bp.route("/bg-upload-extended", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per hour", methods=["POST"])
def upload_extended():
    if current_user.active_role != "coordinator":
        abort(403)
    form = ExtendedBgDetailsForm()
    live_bgs = BankGuarantee.query.filter_by(status=BGStatus.live.value).order_by(
        BankGuarantee.expiry_date
    ).all()
    form.parent_bg_id.choices = [
        (b.id, f"{b.bg_number} - {b.vendor_name or 'vendor'} (expires {b.expiry_date})")
        for b in live_bgs
    ]
    # Pre-select a parent passed from the extension-management page.
    preset = request.args.get("parent", type=int)
    if preset and any(b.id == preset for b in live_bgs):
        form.parent_bg_id.data = preset
    extra_errors = {}
    if request.method == "POST" and form.validate_on_submit():
        parent = db.session.get(BankGuarantee, form.parent_bg_id.data)
        if not parent or parent.status != BGStatus.live.value:
            form.parent_bg_id.errors.append("Selected parent BG is not live.")
        elif form.issue_date.data > parent.expiry_date + timedelta(days=1):
            form.issue_date.errors.append(
                f"The extended BG issue date ({form.issue_date.data}) must be on or before the "
                f"parent's expiry plus one day ({parent.expiry_date + timedelta(days=1)})."
            )
        elif form.expiry_date.data <= parent.expiry_date:
            form.expiry_date.errors.append(
                f"The extended BG expiry date ({form.expiry_date.data}) must be strictly after "
                f"the parent's expiry ({parent.expiry_date})."
            )
        else:
            po_numbers = _po_numbers()
            if not po_numbers:
                extra_errors["po_number"] = "Enter at least one PO number."
            else:
                file_err = _validate_upload_file(request.files.get("file"),
                                                 intake_service.extended_bg_max_bytes())
                if file_err:
                    extra_errors["file"] = file_err
                else:
                    po_context, po_err = _common_po_validation(po_numbers)
                    if po_err:
                        extra_errors["po_number"] = po_err
                    else:
                        bg = _save_and_create(
                            current_user,
                            sap_system_id=parent.sap_system_id,
                            bg_type=parent.bg_type,
                            format_variant=parent.format_variant,
                            expenditure_type=parent.expenditure_type,
                            po_numbers=po_numbers,
                            po_context=po_context,
                            document_type="extended_bg",
                            parent_bg_id=parent.id,
                            provisional_issue=form.issue_date.data,
                            provisional_expiry=form.expiry_date.data,
                        )
                        bg.coordinator_id = current_user.id
                        db.session.commit()
                        audit_service.record(
                            "bg_extension_intake_started",
                            actor_id=current_user.id,
                            target_type="bank_guarantee",
                            target_id=bg.id,
                            metadata_json={"bg_number": bg.bg_number, "parent_bg_id": parent.id},
                        )
                        _enqueue_stage(bg.id, current_user.id, "bg_extraction")
                        return redirect(url_for("intake.upload_extended_progress", bg_id=bg.id))
    sap_names = {s.id: s.display_name for s in SapSystem.query.all()}
    parent_meta = {
        b.id: {
            "bg_number": b.bg_number,
            "sap_system": sap_names.get(b.sap_system_id, "-"),
            "bg_type": b.bg_type.replace("_", " ").title(),
            "format_variant": b.format_variant.title(),
            "expenditure_type": b.expenditure_type.title(),
            "expiry_date": str(b.expiry_date),
        }
        for b in live_bgs
    }
    return render_template(
        "intake/upload_extended.html", form=form, active_nav="upload_extended",
        max_mb=current_app.config["EXTENDED_BG_MAX_MB"], parent_meta=parent_meta,
        extra_errors=extra_errors,
    )


@bp.route("/bg-upload-extended/<int:bg_id>/progress")
@login_required
def upload_extended_progress(bg_id):
    bg, _ = _load_owned_bg(bg_id, "coordinator")
    return render_template("intake/progress.html", bg=bg, bg_id=bg_id,
                           is_extension=True, active_nav="upload_extended")


@bp.route("/bg-upload-extended/<int:bg_id>/review")
@login_required
def upload_extended_review(bg_id):
    return _render_review(bg_id, is_extension=True)


@bp.route("/bg-upload-extended/<int:bg_id>/save-draft", methods=["POST"])
@login_required
def save_extended_draft(bg_id):
    return _save_draft(bg_id, is_extension=True)


@bp.route("/bg-upload-extended/<int:bg_id>/submit", methods=["POST"])
@login_required
def submit_extended(bg_id):
    return _submit(bg_id, is_extension=True)


@bp.route("/bg-upload-extended/<int:bg_id>/discard", methods=["POST"])
@login_required
def discard_extended(bg_id):
    return _discard(bg_id, is_extension=True)


# ----------------------------------------------------------------- shared logic

def _render_review(bg_id, is_extension):
    bg, _ = _load_owned_bg(bg_id, "coordinator" if is_extension else "creator")
    if not _pipeline_ready(bg_id):
        return redirect(url_for(
            "intake.upload_extended_progress" if is_extension else "intake.upload_bg_progress",
            bg_id=bg_id,
        ))
    form = _prefill_review_form(bg)
    analysis, deviations, po_result, checklist = _load_review_data(bg)
    document = Document.query.filter_by(bank_guarantee_id=bg.id).order_by(Document.id).first()
    return render_template(
        "intake/review.html",
        bg=bg, form=form, is_extension=is_extension, analysis=analysis,
        deviations=deviations, po_result=po_result, checklist=checklist,
        document_id=document.id if document else None,
        po_context=po_result.get("po_context", []) if po_result else [],
        shortfall=po_result.get("shortfall") if po_result else None,
        disclaimer=AI_DISCLAIMER,
        active_nav="upload_extended" if is_extension else "upload_bg",
    )


_CONFIRM_FIELDS = [
    "bg_number", "amount", "currency", "issue_date", "expiry_date",
    "claim_expiry_date", "issuing_bank", "vendor_name",
]


def _prepare_review_form(form, bg):
    missing = Deviation.query.filter_by(
        bank_guarantee_id=bg.id, is_missing_critical_clause=True
    ).all()
    form.acknowledgements.choices = [(d.id, d.clause_reference) for d in missing]
    form.confirmed_fields.choices = [(f, f) for f in _CONFIRM_FIELDS]
    return form


def _prefill_review_form(bg):
    form = _prepare_review_form(IntakeReviewForm(), bg)
    document = Document.query.filter_by(bank_guarantee_id=bg.id).order_by(Document.id).first()
    analysis = DocumentAnalysis.query.filter_by(document_id=document.id).first() if document else None
    fields = (analysis.extracted_fields or {}) if analysis else {}
    form.bg_number.data = fields.get("bg_number") or bg.bg_number
    form.amount.data = fields.get("amount")
    form.currency.data = fields.get("currency") or "INR"
    form.issue_date.data = intake_service.parse_date(fields.get("issue_date")) or bg.issue_date
    form.expiry_date.data = intake_service.parse_date(fields.get("expiry_date")) or bg.expiry_date
    form.claim_expiry_date.data = intake_service.parse_date(fields.get("claim_expiry_date"))
    form.issuing_bank.data = fields.get("issuing_bank") or bg.issuing_bank
    form.vendor_name.data = fields.get("vendor_name") or bg.vendor_name
    return form


def _load_review_data(bg):
    document = Document.query.filter_by(bank_guarantee_id=bg.id).order_by(Document.id).first()
    analysis = DocumentAnalysis.query.filter_by(document_id=document.id).first() if document else None
    deviations = intake_service.all_deviations_for(bg.id)
    po_result = (analysis.po_sap_result or {}) if analysis else {}
    checklist = (analysis.checklist_result or []) if analysis else []
    return analysis, deviations, po_result, checklist


def _collect_review_payload(form):
    confirmed = set(request.form.getlist("confirmed_field"))
    return {
        "extracted": {
            "bg_number": form.bg_number.data,
            "amount": form.amount.data,
            "currency": form.currency.data,
            "issue_date": form.issue_date.data,
            "expiry_date": form.expiry_date.data,
            "claim_expiry_date": form.claim_expiry_date.data,
            "issuing_bank": form.issuing_bank.data,
            "vendor_name": form.vendor_name.data,
        },
        "confirmed": confirmed,
        "acknowledgements": form.acknowledgements.data or [],
        "dispatch": {
            "mode": form.dispatch_mode.data,
            "courier_name": form.courier_name.data,
            "tracking_number": form.tracking_number.data,
            "cmr_deliverer_name": form.cmr_deliverer_name.data,
            "cmr_deliverer_email": form.cmr_deliverer_email.data,
            "cmr_deliverer_mobile": form.cmr_deliverer_mobile.data,
        },
    }


def _save_draft(bg_id, is_extension):
    bg, _ = _load_owned_bg(bg_id, "coordinator" if is_extension else "creator")
    form = _prepare_review_form(IntakeReviewForm(), bg)
    review_url = "intake.upload_extended_review" if is_extension else "intake.upload_bg_review"
    if form.validate_on_submit():
        _persist_extracted_fields(bg, _collect_review_payload(form))
        bg.saved_as_draft = True
        bg.current_stage = "draft_saved"
        db.session.commit()
        audit_service.record(
            "bg_draft_saved",
            actor_id=current_user.id,
            target_type="bank_guarantee",
            target_id=bg.id,
            metadata_json={"bg_number": bg.bg_number},
        )
        flash("Saved as draft. You can resume it any time from Saved Drafts.", "success")
        return redirect(url_for("documents.drafts"))
    flash("Could not save draft - please correct the highlighted fields.", "danger")
    return redirect(url_for(review_url, bg_id=bg.id))


def _submit(bg_id, is_extension):
    bg, _ = _load_owned_bg(bg_id, "coordinator" if is_extension else "creator")
    form = _prepare_review_form(IntakeReviewForm(), bg)
    review_url = "intake.upload_extended_review" if is_extension else "intake.upload_bg_review"
    if not form.validate_on_submit():
        flash("Please correct the highlighted fields before submitting.", "danger")
        return redirect(url_for(review_url, bg_id=bg.id))
    payload = _collect_review_payload(form)
    try:
        po_context = sap_service.get_po_context(bg.po_numbers or [])
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for(review_url, bg_id=bg.id))
    try:
        intake_service.submit_bg(
            bg, user=current_user, extracted_fields=payload["extracted"],
            acknowledgements=payload["acknowledgements"],
            dispatch_data=payload["dispatch"], po_context=po_context,
            is_extension=is_extension,
        )
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for(review_url, bg_id=bg.id))
    return render_template("intake/confirmation.html", bg=bg,
                           is_extension=is_extension, active_nav="dashboard")


def _discard(bg_id, is_extension):
    bg, _ = _load_owned_bg(bg_id, "coordinator" if is_extension else "creator")
    intake_service.discard_draft(bg, current_user)
    flash("Draft discarded.", "info")
    return redirect(url_for("documents.drafts"))


def _persist_extracted_fields(bg, payload):
    document = Document.query.filter_by(bank_guarantee_id=bg.id).order_by(Document.id).first()
    analysis = DocumentAnalysis.query.filter_by(document_id=document.id).first()
    if analysis:
        fields = dict(analysis.extracted_fields or {})
        for key, value in payload["extracted"].items():
            if value is None:
                continue
            if isinstance(value, (datetime, date)):
                value = value.isoformat()
            fields[key] = value
        fields["confirmed_fields"] = list(payload["confirmed"])
        analysis.extracted_fields = fields
    bg.amount = intake_service.parse_money(payload["extracted"]["amount"]) or bg.amount
    bg.issue_date = intake_service.parse_date(payload["extracted"]["issue_date"]) or bg.issue_date
    bg.expiry_date = intake_service.parse_date(payload["extracted"]["expiry_date"]) or bg.expiry_date
    if payload["extracted"].get("claim_expiry_date"):
        bg.claim_expiry_date = intake_service.parse_date(payload["extracted"]["claim_expiry_date"])
    if payload["extracted"].get("issuing_bank"):
        bg.issuing_bank = payload["extracted"]["issuing_bank"]
    if payload["extracted"].get("vendor_name"):
        bg.vendor_name = payload["extracted"]["vendor_name"]
    if payload["extracted"].get("bg_number"):
        bg.bg_number = payload["extracted"]["bg_number"]
