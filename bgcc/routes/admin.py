"""Admin suite & platform configuration (Step 7).

Every route here is admin-only via the Step 1 `admin_required` mechanism. This
closes out the deferred admin work from every prior step: user/role management,
structured editors for every `application_settings` key, the prohibited-clause
override, the audit log viewer, and SAP system management.
"""
import copy
import csv
import io
import re
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from bgcc.extensions import db, limiter
from bgcc.models.audit import AuditLog
from bgcc.models.deviations import Deviation
from bgcc.models.enums import BGType, DeviationTier, PlatformRole
from bgcc.models.jobs import CeleryJob
from bgcc.models.reference import SapSystem
from bgcc.models.settings import ApplicationSetting
from bgcc.models.users import User, UserPreference
from bgcc.services import access_service, audit_service
from bgcc.services.access import admin_required
from bgcc.services.notification_service import dispatch

bp = Blueprint("admin", __name__, url_prefix="/admin")

ROLE_CHOICES = [(r.value, r.name.replace("_", " ").title()) for r in PlatformRole]
TIER_CHOICES = [(t.value, t.value.title()) for t in DeviationTier]
BG_TYPE_CHOICES = [(t.value, t.value.upper()) for t in BGType]


def _get_setting(key):
    s = ApplicationSetting.query.filter_by(setting_key=key).first()
    if s is None:
        s = ApplicationSetting(setting_key=key, setting_value={})
        db.session.add(s)
        db.session.flush()
    return s


def _save_setting(setting, value, reason, extra_meta=None):
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason for this change is required.")
    # Deep-copy so nested dicts are never aliased to the ORM-tracked JSON value.
    setting.setting_value = copy.deepcopy(value)
    old_version = setting.version
    setting.version = (setting.version or 1) + 1
    setting.changed_by = current_user.id
    setting.change_reason = reason
    db.session.commit()
    audit_service.record(
        "config_changed", actor_id=current_user.id, target_type="application_setting",
        target_id=setting.setting_key,
        metadata_json={
            "setting_key": setting.setting_key,
            "old_version": old_version,
            "new_version": setting.version,
            "reason": reason,
            **(extra_meta or {}),
        },
    )
    return setting


# ------------------------------------------------------------- Admin Dashboard

@bp.route("/")
@login_required
@admin_required
def dashboard():
    pending_users = User.query.filter_by(is_approved=False, is_active=True).count()
    pending_overrides = (
        Deviation.query.filter(
            Deviation.effective_tier == "prohibited",
            Deviation.admin_override_by.is_(None),
        ).count()
    )

    cutoff = datetime.utcnow() - timedelta(days=1)
    job_status = {}
    for row in db.session.query(CeleryJob.status, db.func.count()).filter(
        CeleryJob.created_at >= cutoff
    ).group_by(CeleryJob.status).all():
        job_status[row[0].value if hasattr(row[0], "value") else row[0]] = row[1]

    recent_failed = (
        CeleryJob.query.filter(
            CeleryJob.status == "failed",
            CeleryJob.created_at >= cutoff,
        ).order_by(CeleryJob.created_at.desc()).limit(10).all()
    )

    return render_template(
        "admin/dashboard.html",
        pending_users=pending_users, pending_overrides=pending_overrides,
        job_status=job_status, recent_failed=recent_failed, active_nav="admin",
    )


# ------------------------------------------------------------ User management

@bp.route("/users")
@login_required
@admin_required
def users():
    status = request.args.get("status")
    approved = request.args.get("approved")
    role = request.args.get("role")
    business_unit = request.args.get("business_unit")
    q = (request.args.get("q") or "").strip()

    query = User.query
    if status == "active":
        query = query.filter(User.is_active.is_(True))
    elif status == "inactive":
        query = query.filter(User.is_active.is_(False))
    if approved == "yes":
        query = query.filter(User.is_approved.is_(True))
    elif approved == "no":
        query = query.filter(User.is_approved.is_(False))
    if role:
        query = query.filter(User.granted_roles.contains(role))
    if business_unit:
        sys_ids = [s.id for s in SapSystem.query.filter_by(business_unit=business_unit).all()]
        query = query.filter(User.sap_system_id.in_(sys_ids))
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(User.email.ilike(like), User.full_name.ilike(like))
        )
    query = query.order_by(User.created_at.desc())
    page = query.paginate(page=request.args.get("page", 1, type=int), per_page=20, error_out=False)

    pending = User.query.filter_by(is_approved=False, is_active=True).order_by(
        User.created_at.asc()
    ).all()
    sap_systems = SapSystem.query.filter_by(is_active=True).order_by(SapSystem.display_name).all()
    business_units = sorted({s.business_unit for s in SapSystem.query.all() if s.business_unit})

    return render_template(
        "admin/users.html", page=page, pending=pending, sap_systems=sap_systems,
        business_units=business_units,
        filters={"status": status, "approved": approved, "role": role,
                 "business_unit": business_unit, "q": q},
        role_choices=ROLE_CHOICES, active_nav="admin",
    )


def _approve_user(user, roles, sap_system_id):
    from bgcc.utils.urls import build_absolute_url

    role_values = [r for r in roles if r in {c[0] for c in ROLE_CHOICES}]
    user.granted_roles = role_values
    if user.active_role not in role_values:
        user.active_role = role_values[0] if len(role_values) == 1 else None
    if sap_system_id:
        user.sap_system_id = sap_system_id
    user.is_approved = True
    user.is_active = True
    db.session.commit()
    login_url = build_absolute_url("/")
    dispatch(
        user_id=user.id, notification_type="account_approved",
        title="Your access has been approved",
        body="Welcome to BG Command Centre. You can now sign in and start working.",
        link_url=login_url, email_to=user.email,
        email_subject="Your BG Command Centre access has been approved",
        email_body=f"Hi {user.full_name},\n\nYour BG Command Centre account has been approved. "
                   f"You can now sign in and start working at:\n\nDirect link: {login_url}",
        template_name="emails/account_approved.html",
        template_context={
            "recipient_name": user.full_name,
            "recipient_email": user.email,
            "assigned_roles": ", ".join(user.granted_role_values) if user.granted_role_values else "Authorized User",
            "sap_system_name": user.sap_system.display_name if user.sap_system else None,
            "link_url": login_url,
            "action_url": login_url,
        },
        triggered_by=current_user.id,
    )
    audit_service.record(
        "account_approved", actor_id=current_user.id, target_type="user",
        target_id=user.id,
        metadata_json={"email": user.email, "roles": role_values,
                       "sap_system_id": sap_system_id},
    )


@bp.route("/users/<int:user_id>/approve", methods=["POST"])
@login_required
@admin_required
def approve_user(user_id):
    user = db.session.get(User, user_id)
    if not user or user.is_approved:
        abort(404)
    roles = request.form.getlist("roles")
    sap_system_id = request.form.get("sap_system_id", type=int)
    if not roles:
        flash("Assign at least one role to approve this user.", "danger")
        return redirect(url_for("admin.users"))
    _approve_user(user, roles, sap_system_id)
    flash(f"{user.email} approved.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/reject", methods=["POST"])
@login_required
@admin_required
def reject_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    # Archive, not delete - leave is_approved False and deactivate.
    user.is_active = False
    db.session.commit()
    audit_service.record(
        "registration_rejected", actor_id=current_user.id, target_type="user",
        target_id=user.id, metadata_json={"email": user.email},
    )
    flash(f"{user.email} rejected (archived).", "info")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def edit_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if request.method == "POST":
        roles = request.form.getlist("roles")
        sap_system_id = request.form.get("sap_system_id", type=int)
        is_active = request.form.get("is_active") == "on"
        if not roles:
            flash("A user must have at least one role.", "danger")
            return redirect(url_for("admin.edit_user", user_id=user.id))
        old_roles = list(user.granted_roles or [])
        user.granted_roles = roles
        if user.active_role not in roles:
            user.active_role = roles[0] if len(roles) == 1 else None
        if sap_system_id:
            user.sap_system_id = sap_system_id
        user.is_active = is_active
        db.session.commit()
        audit_service.record(
            "user_updated", actor_id=current_user.id, target_type="user",
            target_id=user.id,
            metadata_json={"email": user.email, "roles": roles,
                           "old_roles": old_roles, "sap_system_id": sap_system_id,
                           "is_active": is_active},
        )
        flash(f"{user.email} updated.", "success")
        return redirect(url_for("admin.users"))
    sap_systems = SapSystem.query.order_by(SapSystem.display_name).all()
    return render_template("admin/user_edit.html", user=user, sap_systems=sap_systems,
                           role_choices=ROLE_CHOICES, active_nav="admin")


# ------------------------------------------------- Platform configuration

@bp.route("/configuration", methods=["GET"])
@login_required
@admin_required
def configuration():
    ctx = {
        "doa": (_get_setting("doa_matrix").setting_value or {}),
        "clause": (_get_setting("active_clause_template").setting_value or {}),
        "patterns": (_get_setting("prohibited_clause_patterns").setting_value or []),
        "checklist": (_get_setting("checklist_definitions").setting_value or {}),
        "banks": (_get_setting("approved_banks").setting_value or {}),
        "extension": (_get_setting("extension_policy").setting_value or {}),
        "invocation": (_get_setting("invocation_policy").setting_value or {}),
        "contacts": (_get_setting("executive_contacts").setting_value or {}),
        "assistant": (_get_setting("policy_reference_content").setting_value or {}),
        "business_units": SapSystem.query.order_by(SapSystem.display_name).all(),
    }
    return render_template(
        "admin/configuration.html", ctx=ctx, role_choices=ROLE_CHOICES,
        tier_choices=TIER_CHOICES, bg_type_choices=BG_TYPE_CHOICES,
        active_nav="admin",
    )


def _parse_repeatable_list(form, prefix):
    """Collect repeatable groups: {prefix}_label_i, {prefix}_value_i, ..."""
    return None


@bp.route("/configuration/doa", methods=["POST"])
@login_required
@admin_required
def config_doa():
    from bgcc.services.workflow_service import DEFAULT_STAGE_SEQUENCE

    setting = _get_setting("doa_matrix")
    value = copy.deepcopy(setting.setting_value or {})
    reason = request.form.get("reason", "")
    try:
        if "stage_sequence" not in value or not value["stage_sequence"]:
            value["stage_sequence"] = copy.deepcopy(DEFAULT_STAGE_SEQUENCE)
        on_tiers = request.form.getlist("ceo_tiers")
        on_tiers = [t for t in on_tiers if t in {c[0] for c in TIER_CHOICES}]
        value.setdefault("ceo_cfo_condition", {})["on_tiers"] = on_tiers
        # deviation_visibility: checkbox grid role x tier
        value.setdefault("deviation_visibility", {})
        for role, _ in ROLE_CHOICES:
            tiers = request.form.getlist(f"vis_{role}")
            tiers = [t for t in tiers if t in {c[0] for c in TIER_CHOICES}]
            value["deviation_visibility"][role] = tiers or ["low", "high", "prohibited"]
        _save_setting(setting, value, reason, extra_meta={"tab": "doa"})
        flash("DoA & approval rules saved.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin.configuration", tab="doa"))


@bp.route("/configuration/clause", methods=["POST"])
@login_required
@admin_required
def config_clause():
    setting = _get_setting("active_clause_template")
    value = copy.deepcopy(setting.setting_value or {})
    reason = request.form.get("reason", "")
    try:
        template_name = (request.form.get("template_name") or "").strip()
        clauses = []
        idx = 0
        while True:
            ref = request.form.get(f"clause_ref_{idx}")
            label = request.form.get(f"clause_label_{idx}")
            text = request.form.get(f"clause_text_{idx}")
            if ref is None and label is None and text is None:
                break
            if (ref or "").strip() or (label or "").strip() or (text or "").strip():
                clauses.append({
                    "reference": (ref or "").strip(),
                    "label": (label or "").strip(),
                    "text": (text or "").strip(),
                })
            idx += 1
        value["name"] = template_name or value.get("name", "Active clause template")
        value["clauses"] = clauses
        # Keep mandatory_clauses in sync for the compliance engine.
        value["mandatory_clauses"] = [c["reference"] for c in clauses if c["reference"]]
        _save_setting(setting, value, reason, extra_meta={"tab": "clause"})
        flash("Clause template saved.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin.configuration", tab="clause"))


@bp.route("/configuration/patterns", methods=["POST"])
@login_required
@admin_required
def config_patterns():
    setting = _get_setting("prohibited_clause_patterns")
    reason = request.form.get("reason", "")
    rules = []
    idx = 0
    try:
        while True:
            pattern = request.form.get(f"pattern_{idx}")
            label = request.form.get(f"pattern_label_{idx}")
            if pattern is None and label is None:
                break
            if (pattern or "").strip() or (label or "").strip():
                p = (pattern or "").strip()
                # Validate regex syntax so a bad pattern can't corrupt config.
                try:
                    re.compile(p)
                except re.error as exc:
                    raise ValueError(f"Invalid pattern on row {idx + 1}: {exc}")
                rules.append({"pattern": p, "reason": (label or "").strip()})
            idx += 1
        _save_setting(setting, rules, reason, extra_meta={"tab": "patterns"})
        flash("Prohibited-clause patterns saved.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin.configuration", tab="patterns"))


@bp.route("/configuration/checklist", methods=["POST"])
@login_required
@admin_required
def config_checklist():
    setting = _get_setting("checklist_definitions")
    value = copy.deepcopy(setting.setting_value or {})
    reason = request.form.get("reason", "")
    try:
        items = []
        idx = 0
        while True:
            label = request.form.get(f"chk_label_{idx}")
            key = request.form.get(f"chk_key_{idx}")
            if label is None and key is None:
                break
            if (label or "").strip() or (key or "").strip():
                bg_types = request.form.getlist(f"chk_types_{idx}")
                items.append({
                    "key": (key or "").strip() or f"item_{idx}",
                    "label": (label or "").strip(),
                    "mandatory": request.form.get(f"chk_mandatory_{idx}") == "on",
                    "bg_types": [t for t in bg_types if t in {c[0] for c in BG_TYPE_CHOICES}],
                })
            idx += 1
        value["items"] = items
        _save_setting(setting, value, reason, extra_meta={"tab": "checklist"})
        flash("Checklist definitions saved.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin.configuration", tab="checklist"))


@bp.route("/configuration/banks", methods=["POST"])
@login_required
@admin_required
def config_banks():
    setting = _get_setting("approved_banks")
    value = copy.deepcopy(setting.setting_value or {})
    reason = request.form.get("reason", "")
    try:
        banks = []
        idx = 0
        while True:
            name = request.form.get(f"bank_name_{idx}")
            if name is None:
                break
            if (name or "").strip():
                banks.append({
                    "name": (name or "").strip(),
                    "short_code": (request.form.get(f"bank_code_{idx}") or "").strip(),
                    "contact_email": (request.form.get(f"bank_email_{idx}") or "").strip(),
                })
            idx += 1
        value["banks"] = banks
        _save_setting(setting, value, reason, extra_meta={"tab": "banks"})
        flash("Approved banks saved.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin.configuration", tab="banks"))


@bp.route("/configuration/lifecycle", methods=["POST"])
@login_required
@admin_required
def config_lifecycle():
    reason = request.form.get("reason", "")
    try:
        ext = _get_setting("extension_policy")
        _save_setting(ext, {
            "warning_days": request.form.get("ext_warning_days", type=int),
            "overdue_days": request.form.get("ext_overdue_days", type=int),
        }, reason, extra_meta={"tab": "lifecycle"})

        inv = _get_setting("invocation_policy")
        _save_setting(inv, {
            "approaching_days": request.form.get("inv_approaching_days", type=int),
            "critical_days": request.form.get("inv_critical_days", type=int),
        }, reason, extra_meta={"tab": "lifecycle"})

        contacts = _get_setting("executive_contacts")
        _save_setting(contacts, {
            "cfo_email": (request.form.get("cfo_email") or "").strip(),
            "ceo_email": (request.form.get("ceo_email") or "").strip(),
        }, reason, extra_meta={"tab": "lifecycle"})
        flash("Lifecycle policy saved.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin.configuration", tab="lifecycle"))


@bp.route("/configuration/assistant", methods=["POST"])
@login_required
@admin_required
def config_assistant():
    setting = _get_setting("policy_reference_content")
    value = copy.deepcopy(setting.setting_value or {})
    reason = request.form.get("reason", "")
    try:
        sections = []
        idx = 0
        while True:
            title = request.form.get(f"sec_title_{idx}")
            body = request.form.get(f"sec_body_{idx}")
            if title is None and body is None:
                break
            if (title or "").strip() or (body or "").strip():
                sections.append({
                    "title": (title or "").strip(),
                    "body": (body or "").strip(),
                    "link": (request.form.get(f"sec_link_{idx}") or "").strip() or None,
                })
            idx += 1
        value["sections"] = sections
        _save_setting(setting, value, reason, extra_meta={"tab": "assistant"})
        flash("Policy assistant content saved.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin.configuration", tab="assistant"))


# ------------------------------------------------------------- Business units

@bp.route("/configuration/business-units", methods=["POST"])
@login_required
@admin_required
def config_business_units():
    reason = request.form.get("reason", "")
    try:
        ids = [int(x) if x.isdigit() else 0 for x in request.form.getlist("bu_id")]
        codes = request.form.getlist("bu_code")
        names = request.form.getlist("bu_display")
        units = request.form.getlist("bu_business")
        descriptions = request.form.getlist("bu_description")
        connection_types = request.form.getlist("bu_connection_type")
        statuses = request.form.getlist("bu_status")
        active = request.form.getlist("bu_active")
        active_ids = {int(x) for x in active if x.isdigit()}

        for i in range(len(codes)):
            bu_id = ids[i] if i < len(ids) else 0
            code = (codes[i] if i < len(codes) else "").strip()
            name = (names[i] if i < len(names) else "").strip()
            unit = (units[i] if i < len(units) else "").strip()
            desc = (descriptions[i] if i < len(descriptions) else "").strip()
            conn_type = (connection_types[i] if i < len(connection_types) else "oauth").strip() or "oauth"

            if not code and not name and not unit:
                continue

            if i < len(statuses):
                is_active_val = statuses[i].lower() in ("active", "1", "true")
            elif bu_id:
                is_active_val = bu_id in active_ids
            else:
                is_active_val = True

            if bu_id:
                system = db.session.get(SapSystem, bu_id)
                if not system:
                    continue
                system.code = code or system.code
                system.display_name = name or system.display_name
                system.business_unit = unit or system.business_unit
                if hasattr(system, "description"):
                    system.description = desc
                if hasattr(system, "sap_connection_type"):
                    system.sap_connection_type = conn_type
                system.is_active = is_active_val
            else:
                # New row
                if code and name:
                    new_sys = SapSystem(
                        code=code,
                        display_name=name,
                        business_unit=unit or name,
                        sap_connection_type=conn_type,
                        is_active=is_active_val,
                    )
                    if hasattr(new_sys, "description"):
                        new_sys.description = desc
                    db.session.add(new_sys)
        db.session.commit()
        audit_service.record(
            "business_units_updated", actor_id=current_user.id,
            target_type="application_setting", metadata_json={"reason": reason},
        )
        flash("Business units updated successfully.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("business unit save failed")
        flash("Could not save business units.", "danger")
    return redirect(url_for("admin.configuration", tab="business_units"))


# -------------------------------------------------- Prohibited-clause override

def _unoverridden_prohibited():
    return (
        Deviation.query.filter(
            Deviation.effective_tier == "prohibited",
            Deviation.admin_override_by.is_(None),
        ).order_by(Deviation.created_at.asc()).all()
    )


@bp.route("/prohibited-overrides")
@login_required
@admin_required
def prohibited_overrides():
    deviations = _unoverridden_prohibited()
    bg_map = {}
    rule_map = {}
    from bgcc.models.reference import BankGuarantee
    from bgcc.models.settings import ApplicationSetting

    for d in deviations:
        bg_map[d.bank_guarantee_id] = db.session.get(BankGuarantee, d.bank_guarantee_id)
    return render_template(
        "admin/prohibited_overrides.html", deviations=deviations, bg_map=bg_map,
        active_nav="admin",
    )


@bp.route("/prohibited-overrides/<int:deviation_id>", methods=["GET", "POST"])
@login_required
@admin_required
@limiter.limit("10 per minute", methods=["POST"])
def prohibited_override(deviation_id):
    d = db.session.get(Deviation, deviation_id)
    if not d or d.effective_tier != "prohibited":
        abort(404)
    if d.admin_override_by is not None:
        flash("This deviation already has an override.", "info")
        return redirect(url_for("admin.prohibited_overrides"))
    from bgcc.models.reference import BankGuarantee

    bg = db.session.get(BankGuarantee, d.bank_guarantee_id)
    from bgcc.models.settings import ApplicationSetting

    setting = ApplicationSetting.query.filter_by(setting_key="prohibited_clause_patterns").first()
    rules = setting.setting_value if setting else []
    matched = None
    for rule in rules:
        try:
            if re.search(rule.get("pattern", ""), d.bg_text_excerpt or "", re.IGNORECASE):
                matched = rule
                break
        except re.error:
            continue

    if request.method == "POST":
        reason = (request.form.get("reason") or "").strip()
        confirm = request.form.get("confirm")
        if not reason:
            flash("A substantive written justification is required.", "danger")
            return redirect(url_for("admin.prohibited_override", deviation_id=d.id))
        if confirm != "on":
            flash("You must confirm that this permanently authorizes this clause to proceed.", "danger")
            return redirect(url_for("admin.prohibited_override", deviation_id=d.id))
        d.admin_override_by = current_user.id
        d.admin_override_at = datetime.utcnow()
        d.admin_override_reason = reason
        # effective_tier remains prohibited permanently.
        db.session.commit()
        audit_service.record(
            "prohibited_override_granted", actor_id=current_user.id,
            target_type="deviation", target_id=d.id,
            metadata_json={"bg_number": bg.bg_number if bg else None,
                           "clause_reference": d.clause_reference,
                           "reason": reason},
        )
        flash("Override granted. The deviation remains Prohibited but no longer blocks approval.", "success")
        return redirect(url_for("admin.prohibited_overrides"))

    return render_template(
        "admin/prohibited_override.html", d=d, bg=bg, matched=matched,
        active_nav="admin",
    )


# ------------------------------------------------------------- Audit log viewer

@bp.route("/audit-log")
@login_required
@admin_required
def audit_log():
    event_type = request.args.get("event_type")
    actor = (request.args.get("actor") or "").strip()
    target_type = request.args.get("target_type")
    target_id = (request.args.get("target_id") or "").strip()
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    q = (request.args.get("q") or "").strip()

    query = AuditLog.query
    if event_type:
        query = query.filter(AuditLog.event_type == event_type)
    if target_type:
        query = query.filter(AuditLog.target_type == target_type)
    if target_id:
        query = query.filter(AuditLog.target_id == target_id)
    if date_from:
        query = query.filter(AuditLog.created_at >= date_from)
    if date_to:
        query = query.filter(AuditLog.created_at <= date_to + " 23:59:59")
    search_term = q or actor
    if search_term:
        like = f"%{search_term}%"
        actor_ids = [u.id for u in User.query.filter(
            db.or_(User.email.ilike(like), User.full_name.ilike(like))
        ).all()]
        query = query.filter(
            db.or_(
                AuditLog.actor_id.in_(actor_ids) if actor_ids else db.false(),
                AuditLog.target_id.ilike(like),
            )
        )

    query = query.order_by(AuditLog.created_at.desc())
    page = query.paginate(page=request.args.get("page", 1, type=int), per_page=20, error_out=False)

    event_types = [r[0] for r in db.session.query(AuditLog.event_type).distinct().order_by(AuditLog.event_type).all()]
    target_types = [r[0] for r in db.session.query(AuditLog.target_type).distinct().order_by(AuditLog.target_type).all()]
    actor_map = {}
    for uid in {e.actor_id for e in page.items if e.actor_id}:
        u = db.session.get(User, uid)
        if u:
            actor_map[uid] = u.email

    if request.args.get("export") == "csv":
        return _export_audit_csv(query)

    return render_template(
        "admin/audit_log.html", page=page, event_types=event_types,
        target_types=target_types, actor_map=actor_map,
        filters={"event_type": event_type, "actor": actor, "target_type": target_type,
                 "target_id": target_id, "date_from": date_from, "date_to": date_to, "q": q},
        active_nav="admin",
    )


def _export_audit_csv(query):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "created_at", "event_type", "actor_id", "target_type",
                     "target_id", "metadata", "ip_address"])
    for row in query.limit(5000).all():
        writer.writerow([row.id, row.created_at.isoformat() if row.created_at else "",
                         row.event_type, row.actor_id, row.target_type, row.target_id,
                         str(row.metadata_json), row.ip_address])
    output = buf.getvalue()
    return Response(
        output, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )
