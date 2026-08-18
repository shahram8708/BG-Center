from flask import has_request_context, request

from bgcc.extensions import db
from bgcc.models.audit import AuditLog


def _safe_ip():
    if not has_request_context():
        return None
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if ip and "," in ip:
        ip = ip.split(",")[0].strip()
    return ip


def _safe_actor():
    try:
        from flask_login import current_user

        if current_user and current_user.is_authenticated:
            return current_user.id
    except Exception:
        pass
    return None


def record(
    event_type,
    actor_id=None,
    target_type=None,
    target_id=None,
    metadata_json=None,
    ip_address=None,
):
    if actor_id is None:
        actor_id = _safe_actor()
    if ip_address is None:
        ip_address = _safe_ip()
    entry = AuditLog(
        event_type=event_type,
        actor_id=actor_id,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        metadata_json=metadata_json or {},
        ip_address=ip_address,
    )
    db.session.add(entry)
    db.session.commit()
    return entry
