"""BG intake orchestration helpers shared by routes and Celery tasks."""
import os
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from bgcc.extensions import db
from bgcc.models.enums import (
    BGStatus,
    BGType,
    DeviationStatus,
    DeviationTier,
    ExpenditureType,
    FormatVariant,
    JobStatus,
    WorkflowAction,
)
from bgcc.models.dispatches import Dispatch
from bgcc.models.documents import Document, DocumentAnalysis
from bgcc.models.jobs import CeleryJob
from bgcc.models.reference import BankGuarantee
from bgcc.models.deviations import Deviation
from bgcc.models.workflow import WorkflowHistory
from bgcc.services import audit_service
from bgcc.services.prohibited_clauses import effective_tier
from bgcc.utils import files as file_utils

PDF_SIGNATURE = b"%PDF"


def new_bg_max_bytes():
    from flask import current_app

    return current_app.config["NEW_BG_MAX_MB"] * 1024 * 1024


def extended_bg_max_bytes():
    from flask import current_app

    return current_app.config["EXTENDED_BG_MAX_MB"] * 1024 * 1024


def is_pdf(file_storage):
    file_storage.stream.seek(0)
    head = file_storage.stream.read(5)
    file_storage.stream.seek(0)
    return head.startswith(PDF_SIGNATURE)


def save_uploaded_file(file_storage, upload_root):
    original = file_utils.display_filename(file_storage.filename)
    name = file_utils.safe_filename(file_storage.filename)
    path = os.path.join(upload_root, name)
    file_storage.save(path)
    size = os.path.getsize(path)
    return path, original, size


def create_draft_bg(user, *, sap_system_id, bg_type, format_variant,
                    expenditure_type, po_numbers, parent_bg_id=None,
                    provisional_vendor=None, bg_number=None):
    bg = BankGuarantee(
        bg_number=bg_number or _next_bg_number(),
        parent_bg_id=parent_bg_id,
        bg_type=bg_type,
        format_variant=format_variant,
        expenditure_type=expenditure_type,
        sap_system_id=sap_system_id,
        po_numbers=po_numbers,
        vendor_name=provisional_vendor,
        amount=Decimal("0.00"),
        currency="INR",
        issue_date=date.today(),
        expiry_date=date.today(),
        status=BGStatus.draft,
        current_stage="validating",
        creator_id=user.id,
        coordinator_id=user.id if user.active_role == "coordinator" else None,
    )
    db.session.add(bg)
    db.session.flush()
    return bg


def _next_bg_number():
    today = datetime.utcnow()
    prefix = f"BG-{today.year}-"
    last = (
        BankGuarantee.query.filter(BankGuarantee.bg_number.like(prefix + "%"))
        .order_by(BankGuarantee.id.desc())
        .first()
    )
    seq = 1
    if last and last.bg_number:
        try:
            seq = int(last.bg_number.rsplit("-", 1)[1]) + 1
        except (ValueError, IndexError):
            seq = 1
    return f"{prefix}{seq:06d}"


def create_document(bg_id, user_id, storage_path, original_filename, size, document_type):
    doc = Document(
        bank_guarantee_id=bg_id,
        document_type=document_type,
        storage_path=storage_path,
        original_filename=original_filename,
        mime_type="application/pdf",
        file_size_bytes=size,
        uploaded_by=user_id,
    )
    db.session.add(doc)
    db.session.flush()
    return doc


def validate_po_same_vendor(po_context):
    if not po_context:
        return None
    vendors = {c["vendor_name"].strip().lower() for c in po_context if c.get("vendor_name")}
    if len(vendors) > 1:
        return "All purchase order numbers must belong to the same vendor."
    return None


def parse_money(value):
    if value is None:
        return None
    cleaned = str(value).replace(",", "").strip()
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def parse_date(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def compute_dispatch_readiness(checklist):
    """ready unless any mandatory item fails, then needs_followup."""
    if not checklist:
        return "pending"
    failed_mandatory = any(
        item.get("mandatory") and not item.get("passed")
        for item in checklist
        if isinstance(item, dict)
    )
    return "needs_followup" if failed_mandatory else "ready"


def compute_risk_summary(bg):
    deviations = Deviation.query.filter_by(bank_guarantee_id=bg.id).all()
    if not deviations:
        return "No deviations identified."
    tiers = [d.effective_tier for d in deviations if d.effective_tier]
    if DeviationTier.prohibited.value in tiers:
        summary = "Contains prohibited-tier clause deviation(s); special handling required at approval."
    elif DeviationTier.high.value in tiers:
        summary = "Contains high-tier clause deviation(s); requires reviewer attention."
    elif tiers:
        summary = "Contains low-tier clause deviation(s); standard review applies."
    else:
        summary = "No deviations identified."
    missing = [d for d in deviations if d.is_missing_critical_clause]
    if missing:
        summary += f" {len(missing)} critical clause(s) missing."
    return summary


def all_deviations_for(bg_id):
    return Deviation.query.filter_by(bank_guarantee_id=bg_id).order_by(Deviation.id).all()


def abg_shortfall(po_context, new_amount):
    """Hard, non-overridable shortfall check for Advance BGs."""
    total_open = sum(
        (parse_money(c.get("open_advance_amount")) or Decimal("0"))
        for c in po_context
    )
    if new_amount is None:
        return False, None
    if new_amount < total_open:
        return True, total_open
    return False, None


def pipeline_stage_names(bg):
    return ["extraction", "po_sap_cross_check", "template_compliance", "finalize"]


def get_pipeline_status(bg):
    stage_names = pipeline_stage_names(bg)
    jobs = CeleryJob.query.filter_by(related_bg_id=bg.id).order_by(CeleryJob.id).all()
    jobs_by_task = {}
    for job in jobs:
        jobs_by_task[job.task_name] = job

    mapping = {
        "extraction": "bg_extraction",
        "po_sap_cross_check": "po_sap_cross_check",
        "template_compliance": "template_compliance",
        "finalize": "finalize_validation",
    }
    stages = []
    for name in stage_names:
        job = jobs_by_task.get(mapping[name])
        stages.append({
            "stage": name,
            "task_name": mapping[name],
            "status": job.status.value if job else "queued",
            "error_message": job.error_message if job else None,
        })

    extraction = next(s for s in stages if s["stage"] == "extraction")
    if extraction["status"] in ("queued", "processing"):
        overall = "validating"
    elif extraction["status"] == "failed":
        overall = "failed"
    elif extraction["status"] == "completed":
        analysis = DocumentAnalysis.query.filter_by(
            document_id=_primary_document_id(bg)
        ).first() if bg else None
        if analysis and not analysis.classification_result.get("is_bank_guarantee"):
            overall = "blocked"
        else:
            remaining = [s for s in stages if s["stage"] != "extraction"]
            if any(s["status"] == "failed" for s in remaining):
                overall = "failed"
            elif any(s["status"] in ("queued", "processing") for s in remaining):
                overall = "validating"
            elif all(s["status"] == "completed" for s in remaining):
                overall = "ready"
            else:
                overall = "validating"
    else:
        overall = "validating"

    return {"bg_id": bg.id, "overall": overall, "stages": stages}


def _primary_document_id(bg):
    doc = (
        Document.query.filter_by(bank_guarantee_id=bg.id)
        .order_by(Document.id)
        .first()
    )
    return doc.id if doc else None


def submit_bg(bg, *, user, extracted_fields, acknowledgements, dispatch_data,
              po_context, is_extension):
    """Finalize an intake into pending_buyer_approval (or same for extended)."""
    # Deterministic ABG shortfall re-validation - a hard, non-overridable block.
    if bg.bg_type == BGType.abg.value:
        shortfall, total_open = abg_shortfall(po_context, parse_money(extracted_fields.get("amount")))
        if shortfall:
            raise ValueError(
                "This advance amount is below the currently open advance "
                f"({total_open}) on the referenced purchase order(s). The "
                "shortfall guardrail cannot be overridden."
            )

    # Re-validate that every missing critical clause is acknowledged.
    missing = Deviation.query.filter_by(
        bank_guarantee_id=bg.id, is_missing_critical_clause=True
    ).all()
    ack_ids = {int(a) for a in acknowledgements if str(a).isdigit()}
    unacked = [d.id for d in missing if d.id not in ack_ids]
    if unacked:
        raise ValueError("Please acknowledge every missing critical clause before submitting.")

    # Apply user-confirmed extracted fields.
    _apply_extracted_fields(bg, extracted_fields)

    # Create the dispatch row.
    mode = dispatch_data.get("mode")
    if mode not in ("courier", "cmr"):
        raise ValueError("Please choose a dispatch mode (courier or CMR).")
    if mode == "courier" and not (dispatch_data.get("courier_name") and dispatch_data.get("tracking_number")):
        raise ValueError("Courier name and tracking number are required for courier dispatch.")
    if mode == "cmr" and not (dispatch_data.get("cmr_deliverer_name") and dispatch_data.get("cmr_deliverer_mobile")):
        raise ValueError("CMR deliverer name and mobile are required for CMR dispatch.")
    db.session.add(Dispatch(
        bank_guarantee_id=bg.id,
        context_type="extension" if is_extension else "intake",
        dispatch_mode=mode,
        courier_name=dispatch_data.get("courier_name") if mode == "courier" else None,
        tracking_number=dispatch_data.get("tracking_number") if mode == "courier" else None,
        cmr_deliverer_name=dispatch_data.get("cmr_deliverer_name") if mode == "cmr" else None,
        cmr_deliverer_email=dispatch_data.get("cmr_deliverer_email") or None,
        cmr_deliverer_mobile=dispatch_data.get("cmr_deliverer_mobile") if mode == "cmr" else None,
        dispatched_by=user.id,
    ))

    bg.status = BGStatus.pending_buyer_approval
    bg.current_stage = BGStatus.pending_buyer_approval.value
    bg.updated_at = datetime.utcnow()
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id,
        from_stage=None,
        to_stage=BGStatus.pending_buyer_approval.value,
        action=WorkflowAction.submit,
        actor_id=user.id,
        actor_role=user.active_role,
    ))
    db.session.commit()

    audit_service.record(
        "bg_submitted",
        actor_id=user.id,
        target_type="bank_guarantee",
        target_id=bg.id,
        metadata_json={"bg_number": bg.bg_number, "is_extension": is_extension,
                        "dispatch_mode": mode},
    )

    # Notify buyer-role users scoped to the BG's SAP system.
    _notify_buyers(bg, user)

    # Additive Step 4 linkage: for extensions, connect to the parent's request.
    if is_extension and bg.parent_bg_id:
        from bgcc.services.extension_service import link_uploaded_extension
        from bgcc.models.reference import BankGuarantee as _BG

        parent = db.session.get(_BG, bg.parent_bg_id)
        if parent:
            link_uploaded_extension(parent, bg)

    return bg


def _apply_extracted_fields(bg, fields):
    amount = parse_money(fields.get("amount")) or bg.amount
    bg.amount = amount
    issue = parse_date(fields.get("issue_date")) or bg.issue_date
    expiry = parse_date(fields.get("expiry_date")) or bg.expiry_date
    bg.issue_date = issue
    bg.expiry_date = expiry
    if fields.get("claim_expiry_date"):
        bg.claim_expiry_date = parse_date(fields.get("claim_expiry_date"))
    if fields.get("issuing_bank"):
        bg.issuing_bank = fields.get("issuing_bank")
    if fields.get("vendor_name"):
        bg.vendor_name = fields.get("vendor_name")
    if fields.get("bg_number"):
        bg.bg_number = fields.get("bg_number")


def _notify_buyers(bg, actor):
    from bgcc.models.users import User
    from bgcc.services.notification_service import dispatch as notify

    from bgcc.utils.urls import build_absolute_url

    buyers = User.query.filter(
        User.is_approved.is_(True),
        User.is_active.is_(True),
        User.sap_system_id == bg.sap_system_id,
    ).all()
    detail_path = build_absolute_url(f"/bg/{bg.id}")
    for buyer in buyers:
        if "buyer" in (buyer.granted_roles or []):
            notify(
                user_id=buyer.id,
                notification_type="approval_queue_item",
                title="New Bank Guarantee awaiting review",
                body=f"{bg.bg_number} ({bg.vendor_name or 'vendor'}, {bg.amount} {bg.currency}) "
                     f"has been submitted and awaits your approval.",
                link_url=detail_path,
                email_to=buyer.email,
                email_subject="New BG awaiting your approval",
                email_body=f"{bg.bg_number} has been submitted for your approval.\n\nDirect link: {detail_path}",
                template_name="emails/notification.html",
                template_context={
                    "details": {
                        "Guarantee Number": bg.bg_number,
                        "Vendor": bg.vendor_name or "N/A",
                        "Amount": f"{bg.amount} {bg.currency}" if bg.amount else None,
                    },
                    "action_text": "Review Guarantee",
                    "action_url": detail_path,
                    "link_url": detail_path,
                },
                triggered_by=actor.id,
            )


def primary_document(bg):
    return (
        Document.query.filter_by(bank_guarantee_id=bg.id).order_by(Document.id).first()
    )


def primary_analysis(bg):
    doc = primary_document(bg)
    if not doc:
        return None
    return DocumentAnalysis.query.filter_by(document_id=doc.id).first()


def po_cross_check_result(bg):
    analysis = primary_analysis(bg)
    return (analysis.po_sap_result or {}) if analysis else {}


def format_checklist(bg):
    analysis = primary_analysis(bg)
    return (analysis.checklist_result or []) if analysis else []


def dispatch_readiness(bg):
    analysis = primary_analysis(bg)
    return (analysis.dispatch_readiness if analysis else None) or "pending"


def discard_draft(bg, user):
    """Hard-delete a draft and cascade to documents, analyses, deviations and files."""
    from bgcc.services.files import delete_document_files

    docs = Document.query.filter_by(bank_guarantee_id=bg.id).all()
    for doc in docs:
        analysis = DocumentAnalysis.query.filter_by(document_id=doc.id).first()
        if analysis:
            db.session.delete(analysis)
        delete_document_files(doc)
        db.session.delete(doc)
    Deviation.query.filter_by(bank_guarantee_id=bg.id).delete()
    db.session.delete(bg)
    db.session.commit()
    audit_service.record(
        "bg_draft_discarded",
        actor_id=user.id,
        target_type="bank_guarantee",
        target_id=bg.id,
        metadata_json={"bg_number": bg.bg_number},
    )
