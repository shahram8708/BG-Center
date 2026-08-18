"""BG invocation Celery tasks (Step 5).

Draft generation (Gemini + both document renderings), CEO/hold magic-link
dispatch, and the actual bank/internal notification after the dual gate clears.
Every user-triggered action is tracked with its own `celery_jobs` row. The send
itself is never synchronous in a request handler.
"""
import os
from datetime import datetime

from bgcc.celery import celery
from bgcc.extensions import db
from bgcc.models.enums import JobStatus, WorkflowAction
from bgcc.models.generated_documents import GeneratedDocument
from bgcc.models.jobs import CeleryJob
from bgcc.models.lifecycle import BgInvocation
from bgcc.models.reference import BankGuarantee
from bgcc.models.workflow import WorkflowHistory
from bgcc.services import audit_service, invocation_service

DRAFT_CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "recipient_bank": {"type": "string"},
        "recipient_branch": {"type": "string"},
        "recipient_address": {"type": "string"},
        "claim_amount_figures": {"type": "string"},
        "claim_deadline": {"type": "string"},
        "invocation_phrasing": {"type": "string"},
        "signing_authority": {"type": "string"},
    },
    "required": [
        "recipient_bank", "recipient_branch", "recipient_address",
        "claim_amount_figures", "claim_deadline", "invocation_phrasing",
        "signing_authority",
    ],
}


def _new_job(task_name, bg_id, user_id):
    job = CeleryJob(task_name=task_name, status=JobStatus.queued,
                    related_bg_id=bg_id, triggered_by=user_id)
    db.session.add(job)
    db.session.commit()
    return job


def _start(job):
    if job and job.status == JobStatus.queued.value:
        job.status = JobStatus.processing.value
        db.session.commit()


def _finish(job):
    if job:
        job.status = JobStatus.completed.value
        job.completed_at = datetime.utcnow()
        db.session.commit()


def _fail(job, exc):
    if job:
        job.status = JobStatus.failed.value
        job.error_message = str(exc)
        db.session.commit()


@celery.task(bind=True, name="invocation.generate_draft", max_retries=2)
def generate_draft(self, bg_id, user_id, job_id=None):
    job = db.session.get(CeleryJob, job_id) if job_id else _new_job("invocation.generate_draft", bg_id, user_id)
    _start(job)
    try:
        from bgcc.services import gemini_service
        from bgcc.services.docx_service import render_invocation_letter
        from bgcc.services.pdf_service import render_invocation_letter_pdf

        bg = db.session.get(BankGuarantee, bg_id)
        if not bg:
            raise RuntimeError("Bank Guarantee not found.")
        inv = invocation_service.get_or_create_invocation(bg)

        gemini_content = gemini_service.generate_structured(
            feature="invocation_letter_content",
            bg_id=bg_id,
            user_id=user_id,
            parts=[(
                f"Create the variable content for a demand letter invoking a bank guarantee.\n"
                f"BG number: {bg.bg_number}\nVendor: {bg.vendor_name}\n"
                f"Issuing bank: {bg.issuing_bank}\nAmount: {bg.amount} {bg.currency}\n"
                f"Guarantee type: {bg.bg_type}\n"
                "Provide the recipient bank/branch/address, the claim amount in figures, "
                "the claim deadline, appropriate legal invocation phrasing for this "
                "guarantee type, and the signing authority. Content you write is data "
                "used in a letter template; do not add any legal boilerplate beyond the "
                "requested variable phrasing."
            )],
            response_schema=DRAFT_CONTENT_SCHEMA,
            system_instruction=gemini_service.BASE_SYSTEM_INSTRUCTION,
        )

        content = invocation_service.build_letter_content(bg, gemini_content)
        from flask import current_app

        folder = current_app.config["GENERATED_FOLDER"]
        os.makedirs(folder, exist_ok=True)
        prior = (
            GeneratedDocument.query.filter_by(
                bank_guarantee_id=bg.id, document_kind=invocation_service.INVOCATION_DOC_KIND
            ).order_by(GeneratedDocument.version.desc()).first()
        )
        version = (prior.version + 1) if prior else 1
        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")

        docx_path = os.path.join(folder, f"invocation_{bg.id}_v{version}_{stamp}.docx")
        pdf_path = os.path.join(folder, f"invocation_{bg.id}_v{version}_{stamp}.pdf")
        render_invocation_letter(content, docx_path)
        render_invocation_letter_pdf(content, pdf_path)

        gen_docx = GeneratedDocument(
            bank_guarantee_id=bg.id, document_kind=invocation_service.INVOCATION_DOC_KIND,
            storage_path=docx_path, file_format="docx", generated_by_user_id=user_id,
            version=version,
        )
        gen_pdf = GeneratedDocument(
            bank_guarantee_id=bg.id, document_kind=invocation_service.INVOCATION_DOC_KIND,
            storage_path=pdf_path, file_format="pdf", generated_by_user_id=user_id,
            version=version,
        )
        db.session.add_all([gen_docx, gen_pdf])
        db.session.flush()

        inv.stage = "draft_generated"
        inv.draft_document_id = gen_docx.id
        db.session.add(WorkflowHistory(
            bank_guarantee_id=bg.id, from_stage=inv.stage, to_stage="draft_generated",
            action=WorkflowAction.invocation_draft_generated, actor_id=user_id,
            actor_role="bu_fc", comments=f"Invocation draft generated (version {version}).",
        ))
        db.session.commit()
        audit_service.record(
            "invocation_draft_generated", actor_id=user_id, target_type="bg_invocation",
            target_id=inv.id,
            metadata_json={"bg_number": bg.bg_number, "version": version,
                           "docx_id": gen_docx.id, "pdf_id": gen_pdf.id},
        )

        # In parallel with sign-and-upload: dispatch the CEO approval email.
        from bgcc.models.users import User

        requester = db.session.get(User, user_id)
        try:
            invocation_service.dispatch_ceo_approval(inv)
        except ValueError as exc:
            from bgcc.services.notification_service import dispatch as notify

            notify(
                user_id=user_id, notification_type="invocation_ceo_email_failed",
                title="CEO approval email could not be sent",
                body=f"The CEO approval email for {bg.bg_number} could not be dispatched: {exc}",
                email_to=requester.email if requester else None,
            )

        _finish(job)
        return {"ok": True, "version": version}
    except Exception as exc:
        _fail(job, exc)
        raise self.retry(exc=exc)


@celery.task(bind=True, name="invocation.notify_and_dispatch_send", max_retries=3)
def notify_and_dispatch_send(self, invocation_id):
    """Actual send step: bank email (if configured) + internal notifications."""
    try:
        inv = db.session.get(BgInvocation, invocation_id)
        if not inv or inv.sent_to_bank_at is None:
            return {"ok": False}
        bg = db.session.get(BankGuarantee, inv.bank_guarantee_id)
        bank_email = invocation_service._bank_contact_email(bg)
        final_pdf = None
        pdf_gen = (
            GeneratedDocument.query.filter_by(
                bank_guarantee_id=bg.id, document_kind=invocation_service.INVOCATION_DOC_KIND,
                file_format="pdf",
            ).order_by(GeneratedDocument.version.desc()).first()
        )
        if pdf_gen and pdf_gen.storage_path and os.path.exists(pdf_gen.storage_path):
            final_pdf = pdf_gen.storage_path

        from bgcc.services.notification_service import dispatch, send_email

        if bank_email:
            subject = f"Invocation claim - {bg.bg_number}"
            body = f"Please find the finalized invocation demand for {bg.bg_number}."
            if not final_pdf:
                body += " (PDF not available - please refer to system)."
            send_email(
                to=bank_email,
                subject=subject,
                body=body,
                template_name="emails/invocation_notice.html",
                template_context={
                    "bg": bg,
                    "bg_number": bg.bg_number,
                    "subject": subject,
                },
            )
        _internal_send_notifications(bg, inv, dispatch, bank_email)
        return {"ok": True}
    except Exception as exc:
        raise self.retry(exc=exc)


def _internal_send_notifications(bg, inv, dispatch, bank_email):
    from bgcc.models.users import User
    from bgcc.utils.urls import build_absolute_url

    manual = " Manual bank dispatch is required - no bank contact email is configured." if not bank_email else ""
    detail_url = build_absolute_url(f"/bg/{bg.id}")
    holders = User.query.filter(
        User.is_approved.is_(True), User.is_active.is_(True),
        User.sap_system_id == bg.sap_system_id,
    ).all()
    for holder in holders:
        if set(holder.granted_roles or []) & {"creator", "coordinator", "bu_fc"}:
            details = {
                "Guarantee Number": bg.bg_number,
                "Status": "Finalized & Sent to Bank",
                "Vendor": bg.vendor_name,
                "Claim Amount": f"{bg.amount} {bg.currency}" if bg.amount else None,
            }
            dispatch(
                user_id=holder.id, notification_type="invocation_sent_to_bank",
                title="Invocation claim sent to bank",
                body=f"The invocation for {bg.bg_number} has cleared both gates and is finalized.{manual}",
                link_url=detail_url, email_to=holder.email,
                email_subject="Invocation claim sent to bank",
                email_body=f"The invocation for {bg.bg_number} has been finalized.{manual}\n\nDirect link: {detail_url}",
                template_name="emails/notification.html",
                template_context={"details": details, "action_text": "View Guarantee Details", "action_url": detail_url, "link_url": detail_url},
                triggered_by=None,
            )


@celery.task(name="invocation.dispatch_ceo_email", max_retries=2)
def dispatch_ceo_email(invocation_id):
    inv = db.session.get(BgInvocation, invocation_id)
    if inv:
        invocation_service.dispatch_ceo_approval(inv)
    return {"ok": True}
