from datetime import datetime

from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from bgcc.extensions import db
from bgcc.models.notifications import Notification
from bgcc.services import audit_service

bp = Blueprint("notifications", __name__, url_prefix="/notifications")


def _owned(n_id):
    n = db.session.get(Notification, int(n_id))
    if not n or n.user_id != current_user.id:
        abort(404)
    return n


@bp.route("/")
@login_required
def index():
    read = request.args.get("read")
    ntype = request.args.get("type")
    query = Notification.query.filter_by(user_id=current_user.id)
    if read == "unread":
        query = query.filter(Notification.is_read.is_(False))
    elif read == "read":
        query = query.filter(Notification.is_read.is_(True))
    if ntype:
        query = query.filter(Notification.notification_type == ntype)
    query = query.order_by(Notification.created_at.desc())
    page = query.paginate(page=request.args.get("page", 1, type=int), per_page=20, error_out=False)

    types = [
        r[0] for r in db.session.query(Notification.notification_type)
        .filter(Notification.user_id == current_user.id)
        .distinct().all()
    ]
    return render_template("notifications/index.html", page=page, types=types,
                           read=read, ntype=ntype, active_nav="notifications")


@bp.route("/<int:n_id>/open")
@login_required
def open_notification(n_id):
    n = _owned(n_id)
    if not n.is_read:
        n.is_read = True
        n.read_at = datetime.utcnow()
        db.session.commit()
    return redirect(n.link_url or url_for("notifications.index"))


@bp.route("/<int:n_id>/mark-read", methods=["POST"])
@login_required
def mark_read(n_id):
    n = _owned(n_id)
    if not n.is_read:
        n.is_read = True
        n.read_at = datetime.utcnow()
        db.session.commit()
    return redirect(request.referrer or url_for("notifications.index"))


@bp.route("/mark-all-read", methods=["POST"])
@login_required
def mark_all_read():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({
        "is_read": True, "read_at": datetime.utcnow(),
    })
    db.session.commit()
    audit_service.record("notifications_mark_all_read", actor_id=current_user.id,
                         target_type="notification")
    return redirect(url_for("notifications.index"))
