import logging
from datetime import datetime

from bgcc.celery import celery
from bgcc.extensions import db
from bgcc.models.enums import JobStatus
from bgcc.models.jobs import CeleryJob
from bgcc.models.notifications import Notification
from bgcc.models.users import User
from bgcc.services.notification_service import send_email, send_push
from bgcc.utils.urls import build_absolute_url, get_base_url

logger = logging.getLogger("bgcc.tasks.notification")


@celery.task(bind=True, name="notification.send", max_retries=3)
def send_notification(
    self,
    job_id,
    user_id=None,
    notification_id=None,
    notification_type=None,
    title=None,
    body=None,
    link_url=None,
    email_to=None,
    email_subject=None,
    email_body=None,
    email_html=None,
    template_name=None,
    template_context=None,
):
    logger.info(
        "Processing send_notification task: job_id=%s, user_id=%s, notification_id=%s, type=%s",
        job_id,
        user_id,
        notification_id,
        notification_type,
    )
    job = db.session.get(CeleryJob, job_id) if job_id else None
    if job and job.status == JobStatus.queued.value:
        job.status = JobStatus.processing.value
        db.session.commit()

    try:
        user = db.session.get(User, user_id) if user_id else None
        if user and user.preferences is None:
            from bgcc.models.users import UserPreference

            db.session.add(UserPreference(user_id=user.id))
            db.session.commit()

        # In-app channel: ensure row exists if not already created
        if user and not notification_id:
            existing = Notification.query.filter_by(
                user_id=user.id,
                title=title,
                notification_type=notification_type or "generic",
            ).first()
            if not existing:
                notification = Notification(
                    user_id=user.id,
                    notification_type=notification_type or "generic",
                    title=title or "",
                    body=body or "",
                    link_url=link_url,
                )
                db.session.add(notification)
                db.session.commit()
                logger.info("Created in-app notification id=%s in worker task for user_id=%s", notification.id, user.id)

        base_url = (template_context or {}).get("base_url") if isinstance(template_context, dict) else None
        base_url = base_url or get_base_url()
        if link_url:
            link_url = build_absolute_url(link_url, base_url=base_url)

        # Email channel: rendered HTML template with plain-text fallback.
        recipient = email_to or (user.email if user else None)
        subject = email_subject or title or "BG Command Centre Notification"
        text = email_body or body or ""

        if recipient:
            resolved_template = template_name
            resolved_context = dict(template_context or {})
            resolved_context.setdefault("base_url", base_url)
            if user:
                resolved_context.setdefault("recipient_name", user.full_name)
                resolved_context.setdefault("recipient_email", user.email)
                resolved_context.setdefault("email", user.email)
                resolved_context.setdefault(
                    "assigned_roles",
                    ", ".join(user.granted_role_values) if user.granted_role_values else "Authorized User",
                )
                if user.sap_system:
                    resolved_context.setdefault("sap_system_name", user.sap_system.display_name)
                    resolved_context.setdefault("sap_system_code", user.sap_system.code)
            resolved_context.setdefault("title", title or subject)
            resolved_context.setdefault("body", text)
            resolved_context.setdefault("link_url", link_url)
            resolved_context.setdefault("action_url", link_url)
            resolved_context.setdefault("notification_type", notification_type)

            for key in ("action_url", "link_url", "reset_url"):
                if key in resolved_context and resolved_context[key]:
                    resolved_context[key] = build_absolute_url(resolved_context[key], base_url=base_url)

            if not resolved_template and not email_html:
                if notification_type == "password_reset":
                    resolved_template = "emails/password_reset.html"
                    resolved_context.setdefault("reset_url", link_url)
                elif notification_type == "account_approved":
                    resolved_template = "emails/account_approved.html"
                elif notification_type == "registration_pending":
                    resolved_template = "emails/registration_pending.html"
                elif notification_type == "extension_digest":
                    resolved_template = "emails/extension_digest.html"
                else:
                    resolved_template = "emails/notification.html"

            logger.info("Sending email to %s (template=%s, subject=%s)", recipient, resolved_template, subject)
            send_email(
                to=recipient,
                subject=subject,
                body=text,
                html_body=email_html,
                template_name=resolved_template,
                template_context=resolved_context,
            )

        # Push channel: real web-push delivery.
        if user:
            send_push(user, notification_type, title, body, link_url)

        if job:
            job.status = JobStatus.completed.value
            job.completed_at = datetime.utcnow()
            db.session.commit()
        logger.info("Notification task job_id=%s completed successfully", job_id)
        return {"ok": True}
    except Exception as exc:
        logger.exception("send_notification task failed for job_id=%s: %s", job_id, exc)
        if job:
            job.status = JobStatus.failed.value
            job.error_message = str(exc)
            db.session.commit()
        raise self.retry(exc=exc)
