from flask import current_app

from bgcc.utils.security_tokens import sign_token, verify_token

PASSWORD_RESET_SALT = "password-reset"
PASSWORD_RESET_MAX_AGE = 60 * 60  # 60 minutes


def generate_password_reset_token(user_id):
    return sign_token(str(user_id), PASSWORD_RESET_SALT, PASSWORD_RESET_MAX_AGE)


def verify_password_reset_token(token):
    return verify_token(token, PASSWORD_RESET_SALT, PASSWORD_RESET_MAX_AGE)


def token_valid_seconds(token):
    max_age = current_app.config.get("PASSWORD_RESET_MAX_AGE", PASSWORD_RESET_MAX_AGE)
    return max_age
