from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


def get_serializer(salt):
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"], salt=salt
    )


def sign_token(payload, salt, max_age):
    return get_serializer(salt).dumps(payload)


def verify_token(token, salt, max_age):
    """Return the payload if the token is valid and unexpired, else None."""
    try:
        return get_serializer(salt).loads(token, max_age=max_age)
    except (SignatureExpired, BadSignature):
        return None
