from flask import Blueprint, redirect, render_template, url_for
from flask_login import current_user, login_required

from bgcc.services.analytics_service import get_scoped_aggregates

bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


@bp.route("/")
@login_required
def index():
    if not current_user.is_approved:
        return redirect(url_for("auth.pending_approval"))
    aggregates = get_scoped_aggregates(current_user)
    # aggregates is None when the hourly cache hasn't warmed yet (fresh deploy).
    return render_template("dashboard/index.html", aggregates=aggregates,
                           active_nav="dashboard")
