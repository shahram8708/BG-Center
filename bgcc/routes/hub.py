from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from bgcc.extensions import db
from bgcc.models.bank_verifications import BankVerification
from bgcc.models.enums import BGStatus, BGType, ExpenditureType
from bgcc.models.reference import BankGuarantee, SapSystem
from bgcc.models.saved_views import SavedView
from bgcc.services import access_service, audit_service, bank_verification_service

bp = Blueprint("hub", __name__, url_prefix="")


# ------------------------------------------------------------- BG Status Hub

@bp.route("/bg-status")
@login_required
def status_hub():
    status = request.args.get("status")
    bg_type = request.args.get("bg_type")
    expenditure = request.args.get("expenditure")
    business_unit = request.args.get("business_unit")
    vendor = (request.args.get("vendor") or "").strip()
    q = (request.args.get("q") or "").strip()
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    view_id = request.args.get("view", type=int)

    if view_id:
        saved = SavedView.query.filter_by(id=view_id, user_id=current_user.id).first()
        if saved:
            state = saved.filter_state or {}
            status = state.get("status")
            bg_type = state.get("bg_type")
            expenditure = state.get("expenditure")
            business_unit = state.get("business_unit")
            vendor = state.get("vendor")
            q = state.get("q")
            date_from = state.get("date_from")
            date_to = state.get("date_to")

    query = BankGuarantee.query
    if status:
        query = query.filter(BankGuarantee.status == status)
    if bg_type:
        query = query.filter(BankGuarantee.bg_type == bg_type)
    if expenditure:
        query = query.filter(BankGuarantee.expenditure_type == expenditure)
    if business_unit:
        sys_ids = [s.id for s in SapSystem.query.filter_by(business_unit=business_unit).all()]
        query = query.filter(BankGuarantee.sap_system_id.in_(sys_ids))
    if vendor:
        query = query.filter(BankGuarantee.vendor_name.ilike(f"%{vendor}%"))
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(BankGuarantee.bg_number.ilike(like), BankGuarantee.vendor_name.ilike(like))
        )
    if date_from:
        query = query.filter(BankGuarantee.issue_date >= date_from)
    if date_to:
        query = query.filter(BankGuarantee.issue_date <= date_to)

    query = query.order_by(BankGuarantee.updated_at.desc())
    page = query.paginate(page=request.args.get("page", 1, type=int), per_page=20, error_out=False)

    saved_views = SavedView.query.filter_by(
        user_id=current_user.id, page_key="status_hub"
    ).order_by(SavedView.name).all()
    business_units = sorted({s.business_unit for s in SapSystem.query.all() if s.business_unit})

    return render_template(
        "hub/status.html",
        page=page, saved_views=saved_views, business_units=business_units,
        filters={"status": status, "bg_type": bg_type, "expenditure": expenditure,
                 "business_unit": business_unit, "vendor": vendor, "q": q,
                 "date_from": date_from, "date_to": date_to},
        status_choices=[(e.value, e.value.replace("_", " ").title()) for e in BGStatus],
        type_choices=[(e.value, e.value.upper()) for e in BGType],
        expenditure_choices=[(e.value, e.value.title()) for e in ExpenditureType],
        active_nav="status_hub",
    )


@bp.route("/bg-status/save-view", methods=["POST"])
@login_required
def status_save_view():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Please name your saved view.", "danger")
        return redirect(url_for("hub.status_hub"))
    state = {
        "status": request.form.get("status") or None,
        "bg_type": request.form.get("bg_type") or None,
        "expenditure": request.form.get("expenditure") or None,
        "business_unit": request.form.get("business_unit") or None,
        "vendor": request.form.get("vendor") or None,
        "q": request.form.get("q") or None,
        "date_from": request.form.get("date_from") or None,
        "date_to": request.form.get("date_to") or None,
    }
    sv = SavedView(user_id=current_user.id, page_key="status_hub", name=name, filter_state=state)
    db.session.add(sv)
    db.session.commit()
    audit_service.record("status_hub_view_saved", actor_id=current_user.id,
                         target_type="saved_view", target_id=sv.id,
                         metadata_json={"name": name})
    flash("View saved.", "success")
    return redirect(url_for("hub.status_hub"))


# ------------------------------------------------------------- Bank Tracker

@bp.route("/bg-bank-tracker")
@login_required
def bank_tracker():
    bgs = BankGuarantee.query.order_by(BankGuarantee.updated_at.desc()).all()
    scoped = []
    for bg in bgs:
        if access_service.can_view_bg(current_user, bg):
            scoped.append(bg)
    verif_map = {}
    for bg in scoped:
        verif_map[bg.id] = bank_verification_service.verification_for(bg)
    return render_template("hub/bank_tracker.html", bgs=scoped, verif_map=verif_map,
                           active_nav="bank_tracker")


@bp.route("/bg-bank-tracker/resend/<int:bg_id>", methods=["POST"])
@login_required
def bank_resend(bg_id):
    if current_user.active_role != "coordinator":
        abort(403)
    bg = db.session.get(BankGuarantee, bg_id)
    if not bg:
        abort(404)
    try:
        bank_verification_service.resend_verification(bg)
        flash("Verification request re-sent to the bank.", "success")
    except Exception as exc:
        flash(f"Could not resend: {exc}", "danger")
    return redirect(url_for("hub.bank_tracker"))


@bp.route("/bg-bank-tracker/override/<int:bg_id>", methods=["POST"])
@login_required
def bank_override(bg_id):
    if current_user.active_role != "coordinator":
        abort(403)
    bg = db.session.get(BankGuarantee, bg_id)
    if not bg:
        abort(404)
    verification = bank_verification_service.verification_for(bg)
    if not verification:
        abort(404)
    status = request.form.get("status")
    reference = request.form.get("reference", "")
    try:
        bank_verification_service.manual_set_status(verification, status, reference, current_user)
        flash("Verification status updated.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("hub.bank_tracker"))
