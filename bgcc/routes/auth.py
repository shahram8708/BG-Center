import logging
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from bgcc.extensions import db, limiter
from bgcc.forms import (
    ForgotPasswordForm,
    RegisterForm,
    ResetPasswordForm,
    RoleSelectForm,
    SignInForm,
)
from bgcc.models.enums import PlatformRole
from bgcc.models.reference import SapSystem
from bgcc.models.users import User, UserPreference
from bgcc.services import audit_service
from bgcc.services.notification_service import dispatch
from bgcc.services.security import (
    generate_password_reset_token,
    verify_password_reset_token,
)

logger = logging.getLogger("bgcc.auth")

bp = Blueprint("auth", __name__, url_prefix="")

# Per-email short-window lockout, independent of the per-IP rate limit.
# In-memory for this step; Step 8 production hardening may back it with shared
# storage. Pruned lazily on each failure so the map cannot grow unbounded.
_failed_attempts = {}
_used_reset_tokens = set()


def _is_locked(email):
    info = _failed_attempts.get(email)
    if info and info.get("until") and datetime.utcnow() < info["until"]:
        return True, info["until"]
    return False, None


def _register_failure(email):
    now = datetime.utcnow()
    limit = current_app.config["LOGIN_ATTEMPT_LIMIT"]
    lock_minutes = current_app.config["LOGIN_LOCKOUT_MINUTES"]
    for key in [k for k, v in _failed_attempts.items() if v.get("until") and v["until"] < now]:
        _failed_attempts.pop(key, None)
    info = _failed_attempts.get(email, {"count": 0, "until": None})
    if info.get("until") and now < info["until"]:
        return
    info["count"] = info.get("count", 0) + 1
    if info["count"] >= limit:
        info["until"] = now + timedelta(minutes=lock_minutes)
        info["count"] = 0
    _failed_attempts[email] = info


def _clear_failures(email):
    _failed_attempts.pop(email, None)


def _safe_next(target):
    if not target or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _redirect_authenticated():
    if current_user.is_approved:
        return redirect(url_for("dashboard.index"))
    return redirect(url_for("auth.pending_approval"))


def _admin_users():
    users = User.query.filter(User.is_active.is_(True), User.is_approved.is_(True)).all()
    return [
        u for u in users
        if u.has_granted_role(PlatformRole.admin.value) or "admin" in (u.granted_roles or [])
    ]


def _notify_admins(notification_type, title, body, email_subject=None, email_body=None, template_name=None, template_context=None):
    from bgcc.utils.urls import build_absolute_url

    admins = _admin_users()
    if not admins:
        logger.warning("No active approved administrators found to notify for %s", notification_type)
        return

    admin_link = build_absolute_url("/admin/users")
    for admin in admins:
        logger.info("Notifying administrator %s (id=%s) [type=%s]", admin.email, admin.id, notification_type)
        dispatch(
            user_id=admin.id,
            notification_type=notification_type,
            title=title,
            body=body,
            link_url=admin_link,
            email_to=admin.email,
            email_subject=email_subject,
            email_body=email_body,
            template_name=template_name,
            template_context=template_context,
            triggered_by=current_user.id if (hasattr(current_user, "is_authenticated") and current_user.is_authenticated) else None,
        )


@bp.route("/", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def sign_in():
    if current_user.is_authenticated:
        return _redirect_authenticated()
    form = SignInForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.query.filter_by(email=email).first()

        locked, until = _is_locked(email)
        if locked:
            audit_service.record(
                "login_failed",
                target_type="user",
                target_id=user.id if user else None,
                metadata_json={"email": email, "reason": "locked"},
            )
            flash(
                "Too many failed attempts. Please try again in a few minutes.",
                "danger",
            )
            return redirect(url_for("auth.sign_in"))

        if user and user.is_active and user.check_password(form.password.data):
            _clear_failures(email)
            user.last_login_at = datetime.utcnow()
            db.session.commit()
            login_user(user, remember=form.remember.data)
            audit_service.record(
                "login_success",
                actor_id=user.id,
                target_type="user",
                target_id=user.id,
                metadata_json={"email": email, "multi_role": user.is_multi_role},
            )
            if not user.is_approved:
                return redirect(url_for("auth.pending_approval"))
            if user.is_multi_role:
                return redirect(url_for("auth.role_select"))
            if not user.active_role:
                user.active_role = user.granted_role_values[0]
                db.session.commit()
            return redirect(_safe_next(request.args.get("next")) or url_for("dashboard.index"))

        _register_failure(email)
        audit_service.record(
            "login_failed",
            target_type="user",
            target_id=user.id if user else None,
            metadata_json={"email": email, "reason": "invalid_credentials"},
        )
        flash("Incorrect email or password.", "danger")
        return redirect(url_for("auth.sign_in"))
    return render_template("auth/sign_in.html", form=form)


@bp.route("/auth/register", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated and current_user.is_approved:
        return redirect(url_for("dashboard.index"))
    form = RegisterForm()
    form.sap_system_id.choices = [
        (s.id, f"{s.display_name} ({s.code})")
        for s in SapSystem.query.filter_by(is_active=True).order_by(SapSystem.display_name).all()
    ]
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        if User.query.filter_by(email=email).first():
            form.email.errors.append("An account with this email already exists.")
        elif not form.sap_system_id.choices:
            form.sap_system_id.errors.append("No SAP system is available yet. Contact your administrator.")
        else:
            user = User(
                email=email,
                full_name=form.full_name.data.strip(),
                granted_roles=form.roles.data,
                active_role=form.roles.data[0] if len(form.roles.data) == 1 else None,
                sap_system_id=form.sap_system_id.data,
                is_approved=False,
                is_active=True,
            )
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.flush()
            if not UserPreference.query.filter_by(user_id=user.id).first():
                db.session.add(UserPreference(user_id=user.id))
            db.session.commit()
            audit_service.record(
                "registration_submitted",
                actor_id=user.id,
                target_type="user",
                target_id=user.id,
                metadata_json={"email": email, "roles": form.roles.data},
            )
            from bgcc.utils.urls import build_absolute_url

            admin_url = build_absolute_url("/admin/users")
            roles_formatted = ", ".join([r.replace("_", " ").title() for r in user.granted_role_values]) if user.granted_role_values else "Standard User"
            sap_name = user.sap_system.display_name if user.sap_system else None
            sap_code = user.sap_system.code if user.sap_system else None

            logger.info("New registration submitted: user_id=%s, email=%s. Triggering admin notifications.", user.id, user.email)
            _notify_admins(
                notification_type="registration_pending",
                title="New registration awaiting approval",
                body=f"{user.full_name} ({user.email}) has requested access to BG Command Centre.",
                email_subject="New BG Command Centre registration awaiting approval",
                email_body=(
                    f"Access Request\n\n"
                    f"Applicant: {user.full_name}\n"
                    f"Work Email: {user.email}\n"
                    f"Requested Roles: {roles_formatted}\n"
                    f"SAP System: {sap_name or 'N/A'}\n\n"
                    "has requested access to BG Command Centre.\n\n"
                    "Please review this request and take the appropriate action from your administration panel.\n\n"
                    f"Direct link: {admin_url}"
                ),
                template_name="emails/registration_pending.html",
                template_context={
                    "applicant_name": user.full_name,
                    "applicant_email": user.email,
                    "roles": roles_formatted,
                    "sap_system_name": sap_name,
                    "sap_system_code": sap_code,
                    "action_url": admin_url,
                    "link_url": admin_url,
                },
            )
            return render_template("auth/register.html", form=form, registered=True)
    return render_template("auth/register.html", form=form, registered=False)


@bp.route("/auth/forgot-password", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def forgot_password():
    if current_user.is_authenticated and current_user.is_approved:
        return redirect(url_for("dashboard.index"))
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.query.filter_by(email=email, is_active=True).first()
        if user:
            from bgcc.utils.urls import build_absolute_url

            token = generate_password_reset_token(user.id)
            reset_url = build_absolute_url(url_for("auth.reset_password", token=token))
            dispatch(
                user_id=user.id,
                notification_type="password_reset",
                title="Reset your password",
                body="A password reset was requested for your account. Follow the link in the email to set a new password.",
                link_url=reset_url,
                email_to=user.email,
                email_subject="Reset your BG Command Centre password",
                email_body=(
                    "We received a request to reset your BG Command Centre password.\n\n"
                    f"Open the link below within 60 minutes to choose a new password:\n{reset_url}\n\n"
                    f"Direct link: {reset_url}\n\n"
                    "If you did not request this, you can safely ignore this email."
                ),
                template_name="emails/password_reset.html",
                template_context={
                    "reset_url": reset_url,
                    "action_url": reset_url,
                    "link_url": reset_url,
                    "email": email,
                    "recipient_email": email,
                },
                triggered_by=None,
            )
            audit_service.record(
                "password_reset_requested",
                actor_id=user.id,
                target_type="user",
                target_id=user.id,
                metadata_json={"email": email},
            )
        flash("If that email has an account, we've sent a reset link.", "info")
        return redirect(url_for("auth.forgot_password"))
    return render_template("auth/forgot_password.html", form=form)


@bp.route("/auth/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if token in _used_reset_tokens:
        return render_template("auth/reset_invalid.html")
    payload = verify_password_reset_token(token)
    if payload is None:
        return render_template("auth/reset_invalid.html")
    user = db.session.get(User, int(payload))
    if not user:
        return render_template("auth/reset_invalid.html")

    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        _used_reset_tokens.add(token)
        db.session.commit()
        login_user(user)
        audit_service.record(
            "password_reset_completed",
            actor_id=user.id,
            target_type="user",
            target_id=user.id,
        )
        flash("Your password has been updated.", "success")
        if not user.is_approved:
            return redirect(url_for("auth.pending_approval"))
        return redirect(url_for("dashboard.index"))
    return render_template("auth/reset_password.html", form=form, token=token)


@bp.route("/auth/pending-approval")
@login_required
def pending_approval():
    if current_user.is_approved:
        return redirect(url_for("dashboard.index"))
    return render_template("auth/pending_approval.html")


@bp.route("/auth/role-select", methods=["GET", "POST"])
@login_required
def role_select():
    if not current_user.is_approved:
        return redirect(url_for("auth.pending_approval"))
    if not current_user.is_multi_role:
        return redirect(url_for("dashboard.index"))
    form = RoleSelectForm()
    form.role.choices = [
        (role, role.replace("_", " ").title()) for role in current_user.granted_role_values
    ]
    if form.validate_on_submit():
        if form.role.data not in current_user.granted_role_values:
            abort(403)
        current_user.active_role = form.role.data
        db.session.commit()
        audit_service.record(
            "role_switched",
            actor_id=current_user.id,
            target_type="user",
            target_id=current_user.id,
            metadata_json={"role": form.role.data},
        )
        flash(f"Working as {form.role.data.replace('_', ' ').title()} now.", "success")
        return redirect(url_for("dashboard.index"))
    return render_template("auth/role_select.html", form=form)


@bp.route("/auth/role-switch", methods=["POST"])
@login_required
def role_switch():
    role = request.form.get("role", "").strip()
    if role not in current_user.granted_role_values:
        abort(403)
    current_user.active_role = role
    db.session.commit()
    audit_service.record(
        "role_switched",
        actor_id=current_user.id,
        target_type="user",
        target_id=current_user.id,
        metadata_json={"role": role},
    )
    flash(f"Switched to {role.replace('_', ' ').title()}.", "success")
    return redirect(request.referrer or url_for("dashboard.index"))


@bp.route("/auth/logout", methods=["POST"])
def logout():
    if current_user.is_authenticated:
        audit_service.record(
            "logout",
            actor_id=current_user.id,
            target_type="user",
            target_id=current_user.id,
        )
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.sign_in"))
