from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required

from bgcc.extensions import db
from bgcc.models.documents import Document, DocumentAnalysis
from bgcc.models.enums import BGStatus
from bgcc.models.generated_documents import GeneratedDocument
from bgcc.models.reference import BankGuarantee
from bgcc.services import (
    access_service,
    audit_service,
    files as file_service,
    intake_service,
)

bp = Blueprint("documents", __name__, url_prefix="/documents")


def _can_view_document(doc):
    if current_user.active_role == "admin":
        return True
    if not doc.bank_guarantee_id:
        return doc.uploaded_by == current_user.id
    bg = db.session.get(BankGuarantee, doc.bank_guarantee_id)
    if bg is None:
        return doc.uploaded_by == current_user.id
    return access_service.can_view_bg(current_user, bg)


def _can_view_generated(gen):
    if current_user.active_role == "admin":
        return True
    if not gen.bank_guarantee_id:
        return False
    bg = db.session.get(BankGuarantee, gen.bank_guarantee_id)
    if bg is None:
        return False
    return access_service.can_view_bg(current_user, bg)


@bp.route("/<int:doc_id>")
@login_required
def view(doc_id):
    doc = db.session.get(Document, int(doc_id))
    if not doc:
        abort(404)
    if not _can_view_document(doc):
        abort(403)
    bg = db.session.get(BankGuarantee, doc.bank_guarantee_id) if doc.bank_guarantee_id else None
    return render_template(
        "documents/viewer.html",
        doc=doc,
        bg=bg,
        raw_url=url_for("documents.raw", doc_id=doc.id),
        active_nav="dashboard",
    )


@bp.route("/<int:doc_id>/raw")
@login_required
def raw(doc_id):
    doc = db.session.get(Document, int(doc_id))
    if not doc:
        abort(404)
    if not _can_view_document(doc):
        abort(403)
    path = file_service.document_path(doc)
    if not path:
        abort(404)
    return send_file(
        path,
        mimetype=doc.mime_type or "application/pdf",
        as_attachment=False,
        download_name=doc.original_filename,
        conditional=True,
    )


@bp.route("/generated")
@login_required
def generated():
    kind = request.args.get("kind")
    bg_number = (request.args.get("q") or "").strip()
    docs = GeneratedDocument.query.order_by(GeneratedDocument.generated_at.desc()).all()
    scoped = [g for g in docs if _can_view_generated(g)]

    bg_map = {}
    for g in scoped:
        if g.bank_guarantee_id and g.bank_guarantee_id not in bg_map:
            bg_map[g.bank_guarantee_id] = db.session.get(BankGuarantee, g.bank_guarantee_id)

    if kind:
        scoped = [g for g in scoped if g.document_kind == kind]
    if bg_number:
        scoped = [
            g for g in scoped
            if bg_map.get(g.bank_guarantee_id)
            and bg_number.lower() in bg_map[g.bank_guarantee_id].bg_number.lower()
        ]

    kinds = sorted({g.document_kind for g in scoped})
    return render_template(
        "documents/generated.html",
        docs=scoped, bg_map=bg_map, kinds=kinds, kind=kind, q=bg_number,
        active_nav="generated",
    )


@bp.route("/generated/<int:gen_id>")
@login_required
def generated_view(gen_id):
    gen = db.session.get(GeneratedDocument, int(gen_id))
    if not gen:
        abort(404)
    if not _can_view_generated(gen):
        abort(403)
    if not gen.storage_path:
        abort(404)
    bg = db.session.get(BankGuarantee, gen.bank_guarantee_id) if gen.bank_guarantee_id else None
    mime = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}.get(
        gen.file_format, "application/octet-stream"
    )
    ext = gen.file_format or "pdf"
    return send_file(
        gen.storage_path,
        mimetype=mime,
        as_attachment=False,
        download_name=f"{bg.bg_number if bg else 'doc'}_{gen.document_kind}_v{gen.version}.{ext}",
        conditional=True,
    )


@bp.route("/drafts")
@login_required
def drafts():
    if current_user.active_role not in ("creator", "coordinator"):
        abort(403)
    query = BankGuarantee.query.filter(
        BankGuarantee.saved_as_draft.is_(True),
        BankGuarantee.status == BGStatus.draft.value,
    )
    if current_user.active_role == "creator":
        query = query.filter(BankGuarantee.creator_id == current_user.id)
    else:
        query = query.filter(BankGuarantee.coordinator_id == current_user.id)
    draft_bgs = query.order_by(BankGuarantee.updated_at.desc()).all()

    drafts_payload = []
    for bg in draft_bgs:
        doc = Document.query.filter_by(bank_guarantee_id=bg.id).order_by(Document.id).first()
        analysis = DocumentAnalysis.query.filter_by(document_id=doc.id).first() if doc else None
        fields = (analysis.extracted_fields or {}) if analysis else {}
        deviations = intake_service.all_deviations_for(bg.id)
        drafts_payload.append({
            "bg": bg,
            "fields": fields,
            "deviations": deviations,
            "is_extension": bool(bg.parent_bg_id),
        })
    return render_template("documents/drafts.html", drafts=drafts_payload, active_nav="drafts")


@bp.route("/drafts/<int:bg_id>/discard", methods=["POST"])
@login_required
def discard(bg_id):
    bg = db.session.get(BankGuarantee, int(bg_id))
    if not bg:
        abort(404)
    is_extension = bool(bg.parent_bg_id)
    expected_role = "coordinator" if is_extension else "creator"
    owner_id = bg.coordinator_id if is_extension else bg.creator_id
    if current_user.active_role != expected_role or owner_id != current_user.id:
        abort(403)
    intake_service.discard_draft(bg, current_user)
    flash("Draft discarded.", "info")
    return redirect(url_for("documents.drafts"))
