from functools import wraps

from flask import abort, redirect, request, url_for
from flask_login import current_user

from bgcc.models.enums import PlatformRole


def _auth_or_redirect():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.sign_in", next=request.path))
    return None


def roles_required(*roles):
    """Restrict a route to one or more active roles (server-side)."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            redirect_to = _auth_or_redirect()
            if redirect_to is not None:
                return redirect_to
            if current_user.active_role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def admin_required(view):
    """Distinct mechanism for admin-only routes."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        redirect_to = _auth_or_redirect()
        if redirect_to is not None:
            return redirect_to
        if current_user.active_role != PlatformRole.admin.value:
            abort(403)
        return view(*args, **kwargs)

    return wrapped
