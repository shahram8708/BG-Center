"""Workflow notification fan-out task.

Stage transitions run synchronously in the request (fast local work); this
Celery task handles the email/in-app notification fan-out so delivery latency
never blocks the approver's response.
"""
from datetime import datetime

from bgcc.celery import celery
from bgcc.extensions import db
from bgcc.models.enums import JobStatus
from bgcc.models.jobs import CeleryJob
from bgcc.models.reference import BankGuarantee
from bgcc.models.users import User
from bgcc.services.notification_service import dispatch
from bgcc.utils.urls import build_absolute_url


def enqueue_fanout(bg, next_role, actor_id, to_live=False, rejection=False):
    job = CeleryJob(
        task_name="workflow.notify_stage_transition",
        status=JobStatus.queued,
        related_bg_id=bg.id,
        triggered_by=actor_id,
    )
    db.session.add(job)
    db.session.commit()
    task = notify_stage_transition.apply_async(kwargs={
        "job_id": job.id,
        "bg_id": bg.id,
        "next_role": next_role,
        "actor_id": actor_id,
        "to_live": to_live,
        "rejection": rejection,
    })
    if task.id:
        job.celery_task_id = task.id
        db.session.commit()
    return job


@celery.task(bind=True, name="workflow.notify_stage_transition", max_retries=3)
def notify_stage_transition(self, job_id, bg_id, next_role, actor_id, to_live=False,
                            rejection=False):
    job = db.session.get(CeleryJob, job_id) if job_id else None
    if job and job.status == JobStatus.queued.value:
        job.status = JobStatus.processing.value
        db.session.commit()
    try:
        bg = db.session.get(BankGuarantee, bg_id) if bg_id else None
        if not bg:
            if job:
                job.status = JobStatus.failed.value
                job.error_message = "BG not found"
                db.session.commit()
            return {"ok": False}

        detail_url = build_absolute_url(f"/bg/{bg.id}")
        if to_live:
            _notify(bg.creator_id, "bg_live", "Your Bank Guarantee is now Live",
                    f"{bg.bg_number} has been verified and activated. It is now Live.",
                    detail_url, actor_id, bg)
            if bg.coordinator_id:
                _notify(bg.coordinator_id, "bg_live", "A Bank Guarantee you handled is now Live",
                        f"{bg.bg_number} has been verified and activated.", detail_url, actor_id, bg)
        elif rejection:
            _notify(bg.creator_id, "bg_rejected", "Your Bank Guarantee was rejected",
                    f"{bg.bg_number} was rejected during review. You can view the reason on its record.",
                    detail_url, actor_id, bg)
        elif next_role == "ceo_cfo":
            _notify(bg.creator_id, "ceo_cfo_required",
                    "CEO/CFO sign-off required for your Bank Guarantee",
                    f"{bg.bg_number} requires elevated CEO/CFO sign-off. Please obtain it via email and "
                    "attach the evidence in the CEO/CFO Mail page.",
                    detail_url, actor_id, bg)
        else:
            _notify_role(bg, next_role, detail_url, actor_id)

        if job:
            job.status = JobStatus.completed.value
            job.completed_at = datetime.utcnow()
            db.session.commit()
        return {"ok": True}
    except Exception as exc:
        if job:
            job.status = JobStatus.failed.value
            job.error_message = str(exc)
            db.session.commit()
        raise self.retry(exc=exc)


def _notify(user_id, ntype, title, body, link_url, actor_id, bg):
    if not user_id:
        return
    user = db.session.get(User, user_id)
    if user:
        abs_link = build_absolute_url(link_url)
        details = {
            "Guarantee Number": bg.bg_number if bg else None,
            "Vendor": bg.vendor_name if bg else None,
            "Amount": f"{bg.amount} {bg.currency}" if bg and bg.amount else None,
        }
        dispatch(
            user_id=user.id,
            notification_type=ntype,
            title=title,
            body=body,
            link_url=abs_link,
            email_to=user.email,
            email_subject=title,
            email_body=f"{body}\n\nDirect link: {abs_link}",
            template_name="emails/notification.html",
            template_context={"details": details, "action_text": "View Guarantee Details", "action_url": abs_link, "link_url": abs_link},
            triggered_by=actor_id,
        )


def _notify_role(bg, role, detail_url, actor_id):
    abs_link = build_absolute_url(detail_url)
    holders = User.query.filter(
        User.is_approved.is_(True),
        User.is_active.is_(True),
        User.sap_system_id == bg.sap_system_id,
    ).all()
    for holder in holders:
        if role in (holder.granted_roles or []):
            details = {
                "Guarantee Number": bg.bg_number,
                "Awaiting Stage": role_label(role),
                "Vendor": bg.vendor_name,
                "Amount": f"{bg.amount} {bg.currency}" if bg.amount else None,
            }
            dispatch(
                user_id=holder.id,
                notification_type="approval_queue_item",
                title=f"{bg.bg_number} awaits {role_label(role)} review",
                body=f"{bg.bg_number} has been forwarded and awaits your review.",
                link_url=abs_link,
                email_to=holder.email,
                email_subject=f"{bg.bg_number} awaits your review",
                email_body=f"{bg.bg_number} has been forwarded and awaits your review.\n\nDirect link: {abs_link}",
                template_name="emails/notification.html",
                template_context={"details": details, "action_text": f"Review as {role_label(role)}", "action_url": abs_link, "link_url": abs_link},
                triggered_by=actor_id,
            )


def role_label(role):
    return (role or "").replace("_", " ").title()


@celery.task(bind=True, name="workflow.send_executive_approval_email", max_retries=3)
def send_executive_approval_email(self, recipient, subject, body, html_body=None, template_name=None, template_context=None):
    from bgcc.services.notification_service import send_email

    try:
        send_email(
            to=recipient,
            subject=subject,
            body=body,
            html_body=html_body,
            template_name=template_name or "emails/executive_approval.html",
            template_context=template_context,
        )
        return {"ok": True}
    except Exception as exc:
        raise self.retry(exc=exc)


@celery.task(bind=True, name="workflow.send_vendor_extension_email", max_retries=3)
def send_vendor_extension_email(self, vendor_email, bg_number, vendor_name, expiry_date, message=""):
    from bgcc.services.notification_service import send_email

    subject = f"Request to extend Bank Guarantee {bg_number}"
    body = (
        f"Dear {vendor_name},\n\n"
        f"We are writing to request an extension of Bank Guarantee {bg_number}, "
        f"which currently expires on {expiry_date}.\n\n"
    )
    if message:
        body += f"Message: {message}\n\n"
    body += "Please arrange for an extended Bank Guarantee and share it with our team.\n\nBest regards,\nBG Command Centre"

    context = {
        "subject": subject,
        "bg_number": bg_number,
        "vendor_name": vendor_name,
        "expiry_date": expiry_date,
        "message": message,
        "body": body,
    }

    try:
        send_email(
            to=vendor_email,
            subject=subject,
            body=body,
            template_name="emails/vendor_extension.html",
            template_context=context,
        )
        return {"ok": True}
    except Exception as exc:
        raise self.retry(exc=exc)
