from flask import Blueprint, abort, jsonify, request
from flask_login import current_user, login_required

from bgcc.extensions import db
from bgcc.models.deviations import Deviation
from bgcc.models.enums import BGStatus, JobStatus
from bgcc.models.jobs import CeleryJob
from bgcc.models.reference import BankGuarantee
from bgcc.models.users import UserPreference
from bgcc.services import closure_service, intake_service, sap_service

bp = Blueprint("api", __name__, url_prefix="/api")

_STAGE_TASKS = {
    "extraction": "bg_extraction",
    "po_sap_cross_check": "po_sap_cross_check",
    "template_compliance": "template_compliance",
    "finalize": "finalize_validation",
}


def _load_owned_bg(bg_id):
    bg = db.session.get(BankGuarantee, int(bg_id))
    if not bg:
        abort(404)
    is_extension = bool(bg.parent_bg_id)
    expected_role = "coordinator" if is_extension else "creator"
    owner_id = bg.coordinator_id if is_extension else bg.creator_id
    if current_user.active_role != expected_role or owner_id != current_user.id:
        abort(403)
    return bg, is_extension


@bp.route("/pipeline/status/<int:bg_id>")
@login_required
def pipeline_status(bg_id):
    bg, _ = _load_owned_bg(bg_id)
    return jsonify(intake_service.get_pipeline_status(bg))


@bp.route("/pipeline/retry/<int:bg_id>/<stage>", methods=["POST"])
@login_required
def pipeline_retry(bg_id, stage):
    bg, _ = _load_owned_bg(bg_id)
    if stage not in _STAGE_TASKS:
        abort(404)
    task_name = _STAGE_TASKS[stage]

    # If re-running template compliance, drop prior partial deviations so
    # results do not duplicate across retries.
    if stage == "template_compliance":
        Deviation.query.filter_by(bank_guarantee_id=bg.id).delete()

    job = CeleryJob(
        task_name=task_name,
        status=JobStatus.queued,
        related_bg_id=bg.id,
        triggered_by=current_user.id,
    )
    db.session.add(job)
    db.session.commit()

    from bgcc.tasks import ai_tasks

    task_func = getattr(ai_tasks, task_name)
    if stage == "finalize":
        task = task_func.apply_async(args=[None, bg.id, current_user.id, job.id])
    else:
        task = task_func.apply_async(args=[bg.id, current_user.id, job.id])
    if task.id:
        job.celery_task_id = task.id
        db.session.commit()
    return jsonify({"ok": True, "stage": stage})


@bp.route("/po/context/<po_number>")
@login_required
def po_context(po_number):
    if current_user.active_role not in ("creator", "coordinator"):
        abort(403)
    try:
        ctx = sap_service.get_po_context([po_number])
    except ValueError:
        return jsonify({"found": False, "po_number": po_number})
    except Exception:
        return jsonify({"error": "Could not retrieve the underlying financial records. Please try again."}), 502
    if not ctx:
        return jsonify({"found": False, "po_number": po_number})
    c = ctx[0]
    return jsonify({
        "found": True,
        "po_number": c["po_number"],
        "vendor_name": c["vendor_name"],
    })


@bp.route("/closure/eligibility/<int:bg_id>")
@login_required
def closure_eligibility(bg_id):
    if current_user.active_role != "coordinator":
        abort(403)
    bg = db.session.get(BankGuarantee, bg_id)
    if not bg or bg.status != BGStatus.live.value:
        return jsonify({"error": "Only Live Bank Guarantees can be closed."}), 400
    try:
        result = closure_service.compute_eligibility(bg)
    except Exception as exc:
        return jsonify({"error": "Could not retrieve the underlying financial records. Please try again."}), 502
    return jsonify(result)


@bp.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    data = request.get_json(silent=True) or {}
    subscription = data.get("subscription")
    if not subscription or not isinstance(subscription, dict):
        return jsonify({"error": "A valid push subscription object is required."}), 400

    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys")
    if not endpoint or not isinstance(keys, dict) or not keys.get("auth") or not keys.get("p256dh"):
        return jsonify({"error": "Push subscription is missing required endpoint or encryption keys."}), 400

    # Ensure no other user retains this identical endpoint
    other_prefs = UserPreference.query.filter(UserPreference.user_id != current_user.id).all()
    for other in other_prefs:
        if other.push_subscription:
            if isinstance(other.push_subscription, dict) and other.push_subscription.get("endpoint") == endpoint:
                other.push_subscription = None
            elif isinstance(other.push_subscription, list):
                other.push_subscription = [
                    s for s in other.push_subscription
                    if isinstance(s, dict) and s.get("endpoint") != endpoint
                ] or None

    prefs = UserPreference.query.filter_by(user_id=current_user.id).first()
    if prefs is None:
        prefs = UserPreference(user_id=current_user.id)
        db.session.add(prefs)

    prefs.push_subscription = subscription
    prefs.notify_push = True
    db.session.commit()
    return jsonify({"ok": True, "message": "Push notifications enabled successfully."})


@bp.route("/push/unsubscribe", methods=["POST"])
@login_required
def push_unsubscribe():
    prefs = UserPreference.query.filter_by(user_id=current_user.id).first()
    if prefs:
        prefs.push_subscription = None
        prefs.notify_push = False
        db.session.commit()
    return jsonify({"ok": True, "message": "Push notifications disabled."})


@bp.route("/push/vapid-public-key")
@login_required
def push_vapid_public_key():
    from flask import current_app
    return jsonify({"vapid_public_key": current_app.config.get("VAPID_PUBLIC_KEY", "")})


@bp.route("/parent-bg/search")
@login_required
def parent_bg_search():
    if current_user.active_role != "coordinator":
        abort(403)
    q = (request.args.get("q") or "").strip()
    query = BankGuarantee.query.filter_by(status=BGStatus.live.value)
    if q:
        query = query.filter(BankGuarantee.bg_number.ilike(f"%{q}%"))
    results = [
        {
            "id": b.id,
            "bg_number": b.bg_number,
            "vendor_name": b.vendor_name,
            "expiry_date": str(b.expiry_date),
        }
        for b in query.order_by(BankGuarantee.bg_number).limit(20).all()
    ]
    return jsonify({"results": results})
