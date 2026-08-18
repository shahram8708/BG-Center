import os
import traceback

from datetime import datetime

from flask import Flask, jsonify, make_response, render_template, request, send_from_directory, session
from flask_login import current_user

from bgcc.config import config_by_name
from bgcc.extensions import cache, csrf, db, limiter, login_manager, migrate
from bgcc.utils.logging import log_error, request_id


def create_app(config_name=None):
    if config_name is None:
        config_name = os.environ.get("FLASK_CONFIG", "development")
    config_class = config_by_name[config_name]

    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config_class)
    config_class.init_app(app)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "auth.sign_in"
    login_manager.login_message = "Please sign in to continue."
    login_manager.login_message_category = "info"
    csrf.init_app(app)
    limiter.init_app(app)
    cache.init_app(app)

    from bgcc.celery import init_celery

    init_celery(app)

    from bgcc import models  # noqa: F401  (register all models for migrations)
    from bgcc.routes import create_blueprints

    for bp in create_blueprints():
        app.register_blueprint(bp)

    from bgcc.cli import register_cli

    register_cli(app)

    @app.route("/sw.js")
    def sw_root():
        resp = make_response(send_from_directory(app.static_folder, "sw.js"))
        resp.headers["Content-Type"] = "application/javascript"
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    @app.route("/manifest.json")
    def manifest_root():
        return send_from_directory(app.static_folder, "manifest.json")

    if app.config.get("USE_PROXY_FIX", True):
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=1,
            x_proto=1,
            x_host=1,
            x_port=1,
            x_prefix=1,
        )

    _register_jinja(app)
    _register_error_handlers(app)
    _register_context(app)

    with app.app_context():
        _auto_create_database(app)
        _warm_dashboard_cache(app)

    import bgcc.tasks  # noqa: F401  (register celery tasks)

    return app


def _warm_dashboard_cache(app):
    """Ensure a fresh instance never defaults to a 'being prepared' dashboard."""
    try:
        from bgcc.services.analytics_service import warm_dashboard_cache

        warm_dashboard_cache()
    except Exception:
        app.logger.warning("dashboard cache warm on startup skipped")


def _register_jinja(app):
    @app.template_filter("role_label")
    def role_label(value):
        return (value or "").replace("_", " ").title()

    @app.template_filter("datetime_display")
    def datetime_display(value):
        if value is None:
            return ""
        return value.strftime("%d %b %Y, %H:%M")

    @app.template_filter("nl2br")
    def nl2br(value):
        if not value:
            return ""
        from markupsafe import Markup, escape
        return Markup(escape(value).replace("\n", "<br/>\n"))

    @app.template_filter("absolute_url")
    def absolute_url(value):
        if not value:
            return ""
        from bgcc.utils.urls import build_absolute_url

        return build_absolute_url(value)


def _register_context(app):
    @app.context_processor
    def inject_globals():
        from flask import has_request_context
        from bgcc.models.notifications import Notification
        from bgcc.utils.urls import get_base_url

        unread = 0
        recent_notifications = []
        user = None
        if has_request_context() and hasattr(current_user, "is_authenticated") and current_user.is_authenticated:
            user = current_user
            unread = (
                Notification.query.filter_by(user_id=user.id, is_read=False).count()
            )
            recent_notifications = (
                Notification.query.filter_by(user_id=user.id)
                .order_by(Notification.created_at.desc())
                .limit(10)
                .all()
            )
        return {
            "app_name": app.config["COMPANY_NAME"],
            "base_url": get_base_url(),
            "unread_notifications": unread,
            "recent_notifications": recent_notifications,
            "current_user": user,
            "now_year": datetime.utcnow().year,
            "app_version": app.config.get("APP_VERSION", "1.0.0"),
            "vapid_public_key": app.config.get("VAPID_PUBLIC_KEY", ""),
        }


def _register_error_handlers(app):
    @app.before_request
    def make_session_permanent():
        session.permanent = True

    @app.errorhandler(404)
    def not_found(error):
        if _wants_json(request):
            return jsonify({"error": "not_found", "message": "The requested resource was not found."}), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def too_large(error):
        if _wants_json(request):
            return jsonify({"error": "payload_too_large", "message": "The uploaded file exceeds the allowed size."}), 413
        return render_template("errors/413.html"), 413

    @app.errorhandler(500)
    def server_error(error):
        incident_id = request_id()
        log_error("server_error", error, incident=incident_id)
        if _wants_json(request):
            return jsonify({"error": "internal_server_error", "incident_id": incident_id}), 500
        return render_template("errors/500.html", incident_id=incident_id), 500

    @app.errorhandler(403)
    def forbidden(error):
        if _wants_json(request):
            return jsonify({"error": "forbidden", "message": "You do not have permission to do that."}), 403
        return render_template("errors/403.html"), 403


def _wants_json(request):
    accept = request.headers.get("Accept", "")
    return "application/json" in accept


def _auto_create_database(app):
    """Create the SQLite database + all tables on first run (Rule 3)."""
    if os.environ.get("SKIP_AUTO_CREATE"):
        return
    from bgcc.models.enums import (  # noqa: F401
        BGStatus,
        BGType,
        DeviationStatus,
        DeviationTier,
        ExpenditureType,
        FormatVariant,
        JobStatus,
        PlatformRole,
        WorkflowAction,
    )

    db.create_all()
    try:
        with db.engine.connect() as conn:
            cols = [row[1] for row in conn.execute(db.text("PRAGMA table_info(sap_systems)")).fetchall()]
            if cols and "description" not in cols:
                conn.execute(db.text("ALTER TABLE sap_systems ADD COLUMN description TEXT"))
                conn.commit()
    except Exception:
        pass

    from bgcc.services.seed_service import initialize_seed_data

    initialize_seed_data(app)

