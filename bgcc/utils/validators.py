import re

from flask import current_app
from wtforms import ValidationError


class CompanyEmail:
    """Validate that the email belongs to the configured company domain."""

    def __init__(self, message=None):
        self.message = message

    def __call__(self, form, field):
        value = (field.data or "").strip().lower()
        domain = (current_app.config["COMPANY_EMAIL_DOMAIN"] or "").lower().lstrip("@")
        if not value or not value.endswith("@" + domain):
            raise ValidationError(
                self.message or f"Please use your {domain} work email address."
            )


def validate_password_complexity(form, field):
    password = field.data or ""
    checks = {
        "at least 8 characters": len(password) >= 8,
        "an uppercase letter": bool(re.search(r"[A-Z]", password)),
        "a lowercase letter": bool(re.search(r"[a-z]", password)),
        "a number": bool(re.search(r"\d", password)),
        "a special character": bool(re.search(r"[^A-Za-z0-9]", password)),
    }
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        raise ValidationError(
            "Password must contain " + " and ".join(failed) + "."
        )
