import json
import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage

from flask import current_app, render_template

from bgcc.extensions import db
from bgcc.models.enums import JobStatus
from bgcc.models.jobs import CeleryJob
from bgcc.models.notifications import Notification
from bgcc.models.users import User
from bgcc.utils.urls import build_absolute_url, get_base_url, normalize_plain_text_urls

logger = logging.getLogger("bgcc.notification")


def _sanitize_for_json(val):
    if val is None or isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(val, "isoformat") and callable(val.isoformat):
        return val.isoformat()
    if hasattr(val, "__tablename__") and val.__tablename__ == "users":
        return {
            "id": getattr(val, "id", None),
            "email": getattr(val, "email", None),
            "full_name": getattr(val, "full_name", None),
            "roles": ", ".join(getattr(val, "granted_role_values", [])) if getattr(val, "granted_role_values", None) else "",
        }
    if isinstance(val, dict):
        return {str(k): _sanitize_for_json(v) for k, v in val.items()}
    if isinstance(val, (list, tuple, set)):
        return [_sanitize_for_json(v) for v in val]
    return str(val)


def dispatch(
    user_id,
    notification_type,
    title,
    body,
    link_url=None,
    email_to=None,
    email_subject=None,
    email_body=None,
    email_html=None,
    template_name=None,
    template_context=None,
    triggered_by=None,
):
    """
    Create an in-app Notification record synchronously in the database,
    create a CeleryJob tracking row, and enqueue the email/push delivery task via Celery.
    """
    base_url = get_base_url()
    if link_url:
        link_url = build_absolute_url(link_url, base_url=base_url)

    ctx_copy = dict(template_context or {})
    ctx_copy.setdefault("base_url", base_url)
    for key in ("link_url", "action_url", "reset_url"):
        if key in ctx_copy and ctx_copy[key]:
            ctx_copy[key] = build_absolute_url(ctx_copy[key], base_url=base_url)
    if link_url and "action_url" not in ctx_copy:
        ctx_copy["action_url"] = link_url
    if link_url and "link_url" not in ctx_copy:
        ctx_copy["link_url"] = link_url

    if email_body:
        email_body = normalize_plain_text_urls(email_body, base_url=base_url)

    notification = None
    if user_id:
        try:
            notification = Notification(
                user_id=user_id,
                notification_type=notification_type or "generic",
                title=title,
                body=body,
                link_url=link_url,
            )
            db.session.add(notification)
            db.session.commit()
            logger.info(
                "Created in-app notification id=%s for user_id=%s [type=%s]: %s",
                notification.id,
                user_id,
                notification_type,
                title,
            )
        except Exception as exc:
            db.session.rollback()
            logger.exception("Failed to create in-app notification for user_id=%s: %s", user_id, exc)

    job = CeleryJob(
        task_name="notification.send",
        status=JobStatus.queued,
        triggered_by=triggered_by,
    )
    db.session.add(job)
    db.session.commit()

    safe_context = _sanitize_for_json(ctx_copy)

    from bgcc.tasks.notification_tasks import send_notification

    try:
        task = send_notification.apply_async(
            kwargs={
                "job_id": job.id,
                "user_id": user_id,
                "notification_id": notification.id if notification else None,
                "notification_type": notification_type,
                "title": title,
                "body": body,
                "link_url": link_url,
                "email_to": email_to,
                "email_subject": email_subject,
                "email_body": email_body,
                "email_html": email_html,
                "template_name": template_name,
                "template_context": safe_context,
            }
        )
        if task and task.id:
            job.celery_task_id = task.id
            db.session.commit()
            logger.info(
                "Enqueued notification Celery task %s (job_id=%s) for user_id=%s (recipient=%s)",
                task.id,
                job.id,
                user_id,
                email_to or user_id,
            )
    except Exception as exc:
        logger.warning(
            "Celery async dispatch failed for job_id=%s: %s. Attempting direct fallback delivery.",
            job.id,
            exc,
        )
        try:
            recipient = email_to
            if not recipient and user_id:
                user = db.session.get(User, user_id)
                recipient = user.email if user else None

            if recipient:
                send_email(
                    to=recipient,
                    subject=email_subject or title,
                    body=email_body or body,
                    html_body=email_html,
                    template_name=template_name,
                    template_context=safe_context,
                )
            job.status = JobStatus.completed.value
            job.completed_at = datetime.utcnow()
            db.session.commit()
            logger.info("Fallback email delivery completed for job_id=%s", job.id)
        except Exception as fallback_exc:
            logger.exception("Fallback email delivery failed for job_id=%s: %s", job.id, fallback_exc)
            job.status = JobStatus.failed.value
            job.error_message = str(fallback_exc)
            db.session.commit()

    return job


def render_html_email(subject, body=None, template_name=None, context=None):
    """Render a responsive HTML email using specified template or default notification template."""
    ctx = dict(context or {})
    base_url = ctx.get("base_url") or get_base_url()
    ctx["base_url"] = base_url
    ctx.setdefault("subject", subject)
    ctx.setdefault("current_year", datetime.utcnow().year)
    if "config" not in ctx:
        ctx["config"] = current_app.config

    for key in ("action_url", "link_url", "reset_url"):
        if key in ctx and ctx[key]:
            ctx[key] = build_absolute_url(ctx[key], base_url=base_url)

    if "action_url" not in ctx and ctx.get("link_url"):
        ctx["action_url"] = ctx["link_url"]
    if "link_url" not in ctx and ctx.get("action_url"):
        ctx["link_url"] = ctx["action_url"]

    if template_name:
        try:
            rendered = render_template(template_name, **ctx)
            logger.info("Successfully rendered email template %s for subject '%s' (%d bytes)", template_name, subject, len(rendered))
            return rendered
        except Exception:
            logger.exception("Failed to render custom email template %s, falling back to notification.html", template_name)

    ctx.setdefault("title", subject)
    ctx.setdefault("body", body or "")
    rendered = render_template("emails/notification.html", **ctx)
    logger.info("Rendered default notification.html template for subject '%s' (%d bytes)", subject, len(rendered))
    return rendered


def send_email(
    to,
    subject,
    body=None,
    html_body=None,
    template_name=None,
    template_context=None,
):
    """Send multipart (HTML + text fallback) email via SMTP or log when unconfigured."""
    config = current_app.config
    base_url = (template_context or {}).get("base_url") if isinstance(template_context, dict) else None
    base_url = base_url or get_base_url()
    
    if not html_body:
        html_body = render_html_email(
            subject=subject,
            body=body,
            template_name=template_name,
            context=template_context,
        )

    plain_text = body or subject or "Please view this message in an HTML-compatible email client."
    plain_text = normalize_plain_text_urls(plain_text, base_url=base_url)

    target_link = None
    if isinstance(template_context, dict):
        target_link = template_context.get("action_url") or template_context.get("link_url") or template_context.get("reset_url")
    if target_link:
        abs_target = build_absolute_url(target_link, base_url=base_url)
        if abs_target and abs_target != base_url and abs_target not in plain_text:
            plain_text = f"{plain_text}\n\nDirect link: {abs_target}"

    if not config.get("SMTP_HOST"):
        logger.info(
            "EMAIL-FALLBACK (SMTP not configured) To=%s Subject=%s [HTML Rendered: %d bytes]\n%s",
            to,
            subject,
            len(html_body) if html_body else 0,
            plain_text,
        )
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.get("SMTP_FROM", "no-reply@bgcc.local")
    message["To"] = to
    message.set_content(plain_text)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    smtp_host = config.get("SMTP_HOST")
    smtp_port = int(config.get("SMTP_PORT", 587))
    use_ssl = config.get("SMTP_USE_SSL", False)
    use_tls = config.get("SMTP_USE_TLS", True)
    smtp_user = config.get("SMTP_USER")
    smtp_password = config.get("SMTP_PASSWORD")

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as server:
                if smtp_user and smtp_password:
                    server.login(smtp_user, smtp_password)
                server.send_message(message)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                if use_tls:
                    server.starttls()
                if smtp_user and smtp_password:
                    server.login(smtp_user, smtp_password)
                server.send_message(message)
        logger.info("EMAIL-SENT successfully via SMTP to %s (Subject: %s)", to, subject)
    except Exception as exc:
        logger.exception("EMAIL-FAILED to send via SMTP to %s (Subject: %s): %s", to, subject, exc)
        raise


def send_push_to_subscription(subscription, title, body, link_url=None):
    """Send a real web-push notification to a browser subscription."""
    config = current_app.config
    vapid_public = config.get("VAPID_PUBLIC_KEY")
    vapid_private = config.get("VAPID_PRIVATE_KEY")
    if not (vapid_public and vapid_private) or not subscription:
        logger.info(
            "PUSH-SKIP title=%s (VAPID unconfigured or no subscription)",
            title,
        )
        return
    try:
        from pywebpush import webpush

        payload = json.dumps({
            "title": title,
            "body": body,
            "url": link_url or "/",
        })
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=vapid_private,
            vapid_claims={"sub": "mailto:" + config.get("VAPID_CLAIMS_EMAIL", "admin@bg.center")},
        )
        logger.info("PUSH-SENT title=%s", title)
    except Exception:
        logger.exception("PUSH-FAILED title=%s", title)


def send_push(user, notification_type, title, body, link_url=None):
    """Genuine web-push delivery via the user's stored push subscription."""
    if not user or not user.preferences:
        return
    if not user.preferences.notify_push:
        return
    send_push_to_subscription(user.preferences.push_subscription, title, body, link_url)
