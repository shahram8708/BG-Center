from flask import Blueprint, render_template

from bgcc.content import AI_DISCLAIMER

bp = Blueprint("legal", __name__, url_prefix="")


@bp.route("/about")
def about():
    return render_template("legal/about.html")


@bp.route("/legal/disclaimer")
def disclaimer():
    return render_template("legal/disclaimer.html", disclaimer=AI_DISCLAIMER)


@bp.route("/legal/privacy")
def privacy():
    return render_template("legal/privacy.html")


@bp.route("/legal/terms")
def terms():
    return render_template("legal/terms.html")


@bp.route("/offline")
def offline():
    return render_template("errors/offline.html")
