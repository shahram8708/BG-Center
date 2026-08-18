from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from bgcc.extensions import db
from bgcc.models.users import UserPreference
from bgcc.services import audit_service
from bgcc.utils.validators import validate_password_complexity

bp = Blueprint("profile", __name__, url_prefix="/profile")


def _prefs():
    prefs = UserPreference.query.filter_by(user_id=current_user.id).first()
    if prefs is None:
        prefs = UserPreference(user_id=current_user.id)
        db.session.add(prefs)
        db.session.commit()
    return prefs


@bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    prefs = _prefs()
    tab = request.args.get("tab", "account")

    password_error = None
    password_success = False
    if request.method == "POST" and request.form.get("form") == "password":
        target_tab = request.form.get("tab", "account")
        current_pw = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not current_user.check_password(current_pw):
            password_error = "Your current password is incorrect."
        elif new_pw != confirm:
            password_error = "New passwords do not match."
        else:
            class _Field:
                pass
            f = _Field()
            f.data = new_pw
            try:
                validate_password_complexity(None, f)
            except Exception as exc:
                password_error = str(exc)
            else:
                current_user.set_password(new_pw)
                db.session.commit()
                audit_service.record("password_changed", actor_id=current_user.id,
                                     target_type="user", target_id=current_user.id)
                flash("Your password has been updated.", "success")
                return redirect(url_for("profile.index", tab=target_tab))

    prefs_error = None
    prefs_success = False
    if request.method == "POST" and request.form.get("form") == "preferences":
        target_tab = request.form.get("tab", "notifications")
        language = request.form.get("language")
        date_format = request.form.get("date_format")
        if language not in ("en", "hi"):
            prefs_error = "Invalid language selection."
        else:
            prefs.language = language
            prefs.date_format = date_format or "%d %b %Y"
            prefs.notify_email = request.form.get("notify_email") == "on"
            prefs.notify_in_app = request.form.get("notify_in_app") == "on"
            prefs.notify_push = request.form.get("notify_push") == "on"
            db.session.commit()
            audit_service.record("preferences_updated", actor_id=current_user.id,
                                 target_type="user", target_id=current_user.id)
            flash("Preferences saved.", "success")
            return redirect(url_for("profile.index", tab=target_tab))

    return render_template(
        "profile/index.html", tab=tab, prefs=prefs,
        password_error=password_error, password_success=password_success,
        prefs_error=prefs_error, prefs_success=prefs_success,
        active_nav="profile",
    )


@bp.route("/role-switch", methods=["POST"])
@login_required
def role_switch():
    role = request.form.get("role", "").strip()
    if role not in current_user.granted_role_values:
        flash("That role is not granted to you.", "danger")
        return redirect(url_for("profile.index", tab="roles"))
    current_user.active_role = role
    db.session.commit()
    audit_service.record("role_switched", actor_id=current_user.id,
                         target_type="user", target_id=current_user.id,
                         metadata_json={"role": role})
    flash(f"Switched to {role.replace('_', ' ').title()}.", "success")
    return redirect(url_for("profile.index", tab="roles"))
