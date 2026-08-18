import os
import pathlib
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-secret-change-me")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _as_bool(os.environ.get("SESSION_COOKIE_SECURE"), False)
    PERMANENT_SESSION_LIFETIME = timedelta(
        hours=int(os.environ.get("SESSION_IDLE_HOURS", "8"))
    )
    REMEMBER_COOKIE_HTTPONLY = True

    WTF_CSRF_TIME_LIMIT = None
    WTF_CSRF_SSL_STRICT = False

    RATE_LIMIT_ENABLED = _as_bool(os.environ.get("RATE_LIMIT_ENABLED"), False)
    RATELIMIT_ENABLED = RATE_LIMIT_ENABLED
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_IN_MEMORY_FALLBACK_ENABLED = True

    CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL") or "memory://"
    CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND") or "cache+memory://"
    CELERY_TASK_ALWAYS_EAGER = _as_bool(os.environ.get("CELERY_TASK_ALWAYS_EAGER"), False)
    CELERY_TASK_EAGER_PROPAGATES = _as_bool(os.environ.get("CELERY_TASK_EAGER_PROPAGATES"), False)

    # Flask-Caching for pre-computed, proactively warmed dashboard aggregates.
    CACHE_TYPE = os.environ.get("CACHE_TYPE", "SimpleCache")
    CACHE_DEFAULT_TIMEOUT = int(os.environ.get("CACHE_DEFAULT_TIMEOUT", "3600"))
    CACHE_KEY_PREFIX = "bgcc_"
    DASHBOARD_CACHE_KEY = "dashboard_aggregates"

    # ---- Web push (VAPID) ----
    VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
    VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
    VAPID_CLAIMS_EMAIL = os.environ.get("VAPID_CLAIMS_EMAIL", "admin@bg.center")

    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM", "no-reply@bgcc.local")
    SMTP_USE_TLS = _as_bool(os.environ.get("SMTP_USE_TLS"), True)
    SMTP_USE_SSL = _as_bool(os.environ.get("SMTP_USE_SSL"), False)

    COMPANY_NAME = os.environ.get("COMPANY_NAME", "BG Command Centre")
    COMPANY_EMAIL_DOMAIN = os.environ.get("COMPANY_EMAIL_DOMAIN", "bg.center")
    BASE_URL = os.environ.get("BASE_URL") or os.environ.get("APP_BASE_URL") or "http://127.0.0.1:5000"
    PREFERRED_URL_SCHEME = os.environ.get("PREFERRED_URL_SCHEME", "http")
    USE_PROXY_FIX = _as_bool(os.environ.get("USE_PROXY_FIX", "True"), True)

    PWA_NAME = os.environ.get("PWA_NAME", "BG Command Centre")
    PWA_SHORT_NAME = os.environ.get("PWA_SHORT_NAME", "BGCC")
    APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")

    # ---- Gemini AI (Step 2) ----
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    # ---- SAP financial integration (Step 2) ----
    # When SAP_PO_ENDPOINT is set, the real endpoint is queried for PO context.
    # Otherwise the local `sap_po_records` dataset is used.
    SAP_PO_ENDPOINT = os.environ.get("SAP_PO_ENDPOINT", "")
    SAP_CLIENT_ID = os.environ.get("SAP_CLIENT_ID", "")
    SAP_CLIENT_SECRET = os.environ.get("SAP_CLIENT_SECRET", "")
    SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "")

    # ---- Upload ceilings (MB) ----
    NEW_BG_MAX_MB = int(os.environ.get("NEW_BG_MAX_MB", "20"))
    EXTENDED_BG_MAX_MB = int(os.environ.get("EXTENDED_BG_MAX_MB", "10"))

    LOGIN_ATTEMPT_LIMIT = int(os.environ.get("LOGIN_ATTEMPT_LIMIT", "5"))
    LOGIN_LOCKOUT_MINUTES = int(os.environ.get("LOGIN_LOCKOUT_MINUTES", "15"))

    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
    GENERATED_FOLDER = os.path.join(BASE_DIR, "generated")

    JSON_SORT_KEYS = False
    TIMEZONE = "Asia/Kolkata"

    @staticmethod
    def init_app(app):
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        os.makedirs(app.config["GENERATED_FOLDER"], exist_ok=True)


class DevelopmentConfig(Config):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + pathlib.Path(BASE_DIR, "bgcc_dev.db").as_posix()
    )


class TestingConfig(Config):
    TESTING = True
    DEBUG = False
    RATE_LIMIT_ENABLED = False
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + pathlib.Path(BASE_DIR, "bgcc_test.db").as_posix()
    )
    WTF_CSRF_ENABLED = False
    CELERY_TASK_ALWAYS_EAGER = True


class ProductionConfig(Config):
    DEBUG = False
    RATE_LIMIT_ENABLED = _as_bool(os.environ.get("RATE_LIMIT_ENABLED"), True)
    RATELIMIT_ENABLED = RATE_LIMIT_ENABLED
    SESSION_COOKIE_SECURE = _as_bool(os.environ.get("SESSION_COOKIE_SECURE"), True)
    CELERY_TASK_ALWAYS_EAGER = _as_bool(os.environ.get("CELERY_TASK_ALWAYS_EAGER"), False)
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL")


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
