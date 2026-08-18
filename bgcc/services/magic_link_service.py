"""Generic magic-link / executive-approval service.

Reusable across any record that carries a hashed-at-rest token column and a
timestamp column (used now for `bg_closures`' CFO/CEO sign-off, and reused in
Step 5 for `bg_invocations` without any change to this code). Tokens are:
cryptographically random, signed for tamper-evidence, time-limited via the
embedded itsdangerous timestamp, single-use (cleared on use), and stored only as
a SHA-256 hash - never in plaintext, and never written to logs.

Recipients are plain email addresses (not required to be platform users); the
single-use link itself is the authentication.
"""
import hashlib
import secrets

from flask import url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from bgcc.extensions import db
from bgcc.models.settings import ApplicationSetting
from bgcc.utils.urls import build_absolute_url, get_base_url

# Registered token targets. Each entry is:
#   key -> {
#     "model", "token_attr", "ts_attr", "salt", "max_age_seconds",
#     "title", "subject", "on_approve": fn(record)->None, "on_decline": fn(record)
#   }
_TOKEN_TARGETS = {}


def register_target(key, model, token_attr, ts_attr, salt, max_age_settings_key,
                    max_age_default, title, subject, on_approve, on_decline=None):
    _TOKEN_TARGETS[key] = {
        "model": model,
        "token_attr": token_attr,
        "ts_attr": ts_attr,
        "salt": salt,
        "max_age_settings_key": max_age_settings_key,
        "max_age_default": max_age_default,
        "title": title,
        "subject": subject,
        "on_approve": on_approve,
        "on_decline": on_decline,
    }


def _max_age(key):
    cfg = _TOKEN_TARGETS[key]
    return _settings_int(cfg["max_age_settings_key"], cfg["max_age_default"])


def _serializer(salt):
    from flask import current_app

    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=salt)


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _executive_email():
    setting = ApplicationSetting.query.filter_by(setting_key="executive_contacts").first()
    value = (setting.setting_value if setting else {}) or {}
    return value


def _settings_int(key, default):
    setting = ApplicationSetting.query.filter_by(setting_key=key).first()
    return int(setting.setting_value) if setting and setting.setting_value is not None else default


def issue_and_email(key, record, recipient_email, extra_lines=None, triggered_by=None):
    """Generate a token, store its hash, and dispatch the email via Celery."""
    cfg = _TOKEN_TARGETS[key]
    raw = secrets.token_urlsafe(32)
    token = _serializer(cfg["salt"]).dumps(
        {"key": key, "record_id": record.id, "nonce": raw}, salt=cfg["salt"]
    )
    setattr(record, cfg["token_attr"], _hash(token))
    db.session.commit()

    base_url = get_base_url()
    try:
        relative_link = url_for("lifecycle.executive_approval", token=token)
        link = build_absolute_url(relative_link, base_url=base_url)
    except Exception:
        link = build_absolute_url(f"/lifecycle/executive-approval?token={token}", base_url=base_url)

    body = (
        f"An approval has been requested in BG Command Centre.\n\n"
        f"{cfg['subject']}\n\n"
    )
    if extra_lines:
        body += "\n".join(str(x) for x in extra_lines) + "\n\n"
    body += (
        f"Approve here: {link}\n\n"
        f"Direct link: {link}\n\n"
        "This link expires and can only be used once. Do not forward it."
    )

    template_name = (
        "emails/bank_verification.html"
        if key == "bank_verification"
        else "emails/executive_approval.html"
    )
    context = {
        "title": cfg.get("title") or cfg["subject"],
        "subject": cfg["subject"],
        "link_url": link,
        "action_url": link,
        "base_url": base_url,
        "extra_lines": extra_lines or [],
        "recipient_email": recipient_email,
    }

    from bgcc.tasks.workflow_tasks import send_executive_approval_email

    send_executive_approval_email.delay(
        recipient=recipient_email,
        subject=cfg["subject"],
        body=body,
        template_name=template_name,
        template_context=context,
    )
    return token


def resolve_token(token):
    """Resolve a raw token to (key, record) or (None, None).

    Finds the record whose stored hash matches, then validates signature and
    expiry. Returns the target context so the view can render appropriately.
    """
    import bgcc.services.bank_verification_service  # noqa: F401
    import bgcc.services.closure_service  # noqa: F401
    import bgcc.services.invocation_service  # noqa: F401

    for key, cfg in _TOKEN_TARGETS.items():
        try:
            payload = _serializer(cfg["salt"]).loads(
                token, salt=cfg["salt"], max_age=_max_age(key)
            )
        except (SignatureExpired, BadSignature):
            continue
        if payload.get("key") != key:
            continue
        record = db.session.get(cfg["model"], int(payload.get("record_id")))
        if record is None:
            continue
        if getattr(record, cfg["token_attr"]) != _hash(token):
            continue
        return key, record
    return None, None


def is_usable(key, record):
    """True if the token is unexpired and not yet used (single-use)."""
    cfg = _TOKEN_TARGETS[key]
    # Already used -> the stored timestamp column is set and the hash cleared.
    if getattr(record, cfg["ts_attr"]) is not None:
        return False
    if getattr(record, cfg["token_attr"]) is None:
        return False
    return True


def token_context(key, record):
    cfg = _TOKEN_TARGETS[key]
    return {
        "title": cfg["title"],
        "subject": cfg["subject"],
    }


def consume_and_apply(key, record):
    """Mark the token used, record the timestamp, run the transition callback."""
    from datetime import datetime

    cfg = _TOKEN_TARGETS[key]
    setattr(record, cfg["ts_attr"], datetime.utcnow())
    setattr(record, cfg["token_attr"], None)
    db.session.commit()
    cfg["on_approve"](record)
    db.session.commit()


def consume_and_decline(key, record):
    cfg = _TOKEN_TARGETS[key]
    setattr(record, cfg["token_attr"], None)
    db.session.commit()
    if cfg.get("on_decline"):
        cfg["on_decline"](record)
        db.session.commit()
