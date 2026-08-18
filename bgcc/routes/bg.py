from flask import Blueprint, abort, current_app, flash, redirect, render_template, url_for
from flask_login import current_user, login_required

from bgcc.extensions import db
from bgcc.models.documents import Document
from bgcc.models.generated_documents import GeneratedDocument
from bgcc.models.reference import BankGuarantee
from bgcc.models.workflow import WorkflowHistory
from bgcc.services import access_service, audit_service, intake_service, report_service, workflow_service

bp = Blueprint("bg", __name__, url_prefix="")


@bp.route("/bg/<int:bg_id>")
@login_required
def detail(bg_id):
    bg = db.session.get(BankGuarantee, bg_id)
    if not bg:
        abort(404)
    if not access_service.can_view_bg(current_user, bg):
        abort(403)
    timeline = WorkflowHistory.query.filter_by(bank_guarantee_id=bg.id).order_by(
        WorkflowHistory.created_at
    ).all()
    documents = Document.query.filter_by(bank_guarantee_id=bg.id).order_by(
        Document.id
    ).all()
    generated = GeneratedDocument.query.filter_by(bank_guarantee_id=bg.id).order_by(
        GeneratedDocument.version
    ).all()
    analysis = intake_service.primary_analysis(bg)
    fields = (analysis.extracted_fields or {}) if analysis else {}
    po_result = intake_service.po_cross_check_result(bg)
    checklist = intake_service.format_checklist(bg)
    deviations = intake_service.all_deviations_for(bg.id)

    authorized = workflow_service.current_authorized_role(bg)
    show_shortcut = (
        authorized == current_user.active_role
        and authorized in workflow_service.ROLE_TO_STATUS
    )

    return render_template(
        "bg/detail.html",
        bg=bg, timeline=timeline, documents=documents, generated=generated,
        fields=fields, po_result=po_result, checklist=checklist, deviations=deviations,
        show_shortcut=show_shortcut, authorized=authorized,
        dispatch_readiness=intake_service.dispatch_readiness(bg),
        active_nav="dashboard",
    )


@bp.route("/bg/<int:bg_id>/generate-report", methods=["POST"])
@login_required
def generate_report(bg_id):
    bg = db.session.get(BankGuarantee, bg_id)
    if not bg:
        abort(404)
    if not access_service.can_view_bg(current_user, bg):
        abort(403)
    try:
        row = report_service.generate_comparison_report(
            bg, current_user, current_app.config["GENERATED_FOLDER"]
        )
    except Exception as exc:
        current_app.logger.exception("comparison report generation failed for bg=%s", bg_id)
        flash("The comparison report could not be generated. Please try again.", "danger")
        return redirect(url_for("bg.detail", bg_id=bg_id))
    audit_service.record(
        "comparison_report_generated", actor_id=current_user.id,
        target_type="bank_guarantee", target_id=bg.id,
        metadata_json={"bg_number": bg.bg_number, "version": row.version},
    )
    flash(f"Comparison Report v{row.version} generated.", "success")
    return redirect(url_for("documents.generated"))
