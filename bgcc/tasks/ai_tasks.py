"""BG intake & validation Celery pipeline (Steps/Features 1–4).

New-BG pipeline:  bg_extraction (Stage 1) -> chord [po_sap_cross_check (2a),
template_compliance (2b)] -> finalize_validation (callback).
Extended pipeline mirrors this, with 2b doing the lighter clause-collision
comparison against the parent's own stored text and 2a including the ABG
shortfall guardrail.
"""
import json
import logging
from datetime import datetime

from celery import chord

from bgcc.celery import celery
from bgcc.extensions import db
from bgcc.models.deviations import Deviation
from bgcc.models.documents import Document, DocumentAnalysis
from bgcc.models.enums import JobStatus
from bgcc.models.jobs import CeleryJob
from bgcc.models.reference import BankGuarantee
from bgcc.models.settings import ApplicationSetting
from bgcc.services import files as file_service
from bgcc.services import intake_service
from bgcc.services import sap_service
from bgcc.services.prohibited_clauses import effective_tier

logger = logging.getLogger("bgcc.intake")


def _new_job(task_name, bg_id, user_id):
    job = CeleryJob(
        task_name=task_name,
        status=JobStatus.queued,
        related_bg_id=bg_id,
        triggered_by=user_id,
    )
    db.session.add(job)
    db.session.commit()
    return job


def _start_job(job):
    if job and job.status == JobStatus.queued.value:
        job.status = JobStatus.processing.value
        db.session.commit()


def _finish_job(job):
    if job:
        job.status = JobStatus.completed.value
        job.completed_at = datetime.utcnow()
        db.session.commit()


def _fail_job(job, exc):
    if job:
        job.status = JobStatus.failed.value
        job.error_message = str(exc)
        db.session.commit()


def _primary_document(bg):
    return (
        Document.query.filter_by(bank_guarantee_id=bg.id)
        .order_by(Document.id)
        .first()
    )


def _analysis_for(document):
    return DocumentAnalysis.query.filter_by(document_id=document.id).first()


def _prohibited_rules():
    setting = ApplicationSetting.query.filter_by(
        setting_key="prohibited_clause_patterns"
    ).first()
    value = setting.setting_value if setting else []
    if isinstance(value, dict):
        value = value.get("rules") or []
    return value or []


def _template_content():
    setting = ApplicationSetting.query.filter_by(
        setting_key="active_clause_template"
    ).first()
    if not setting:
        return "", []
    value = setting.setting_value or {}
    return value.get("body", ""), value.get("mandatory_clauses", [])


def _parent_clauses(parent_bg):
    doc = _primary_document(parent_bg)
    if not doc:
        return []
    analysis = _analysis_for(doc)
    if not analysis:
        return []
    return (analysis.extracted_fields or {}).get("clauses", [])


@celery.task(bind=True, name="bg_extraction", max_retries=2)
def bg_extraction(self, bg_id, user_id, job_id=None):
    job = db.session.get(CeleryJob, job_id) if job_id else _new_job("bg_extraction", bg_id, user_id)
    _start_job(job)
    bg = db.session.get(BankGuarantee, bg_id)
    try:
        from bgcc.services import gemini_service

        document = _primary_document(bg)
        if not document:
            raise RuntimeError("No uploaded document found for this Bank Guarantee.")
        with open(file_service.document_path(document), "rb") as f:
            pdf_bytes = f.read()

        checklist_setting = ApplicationSetting.query.filter_by(
            setting_key="checklist_definitions"
        ).first()
        checklist_defs = (checklist_setting.setting_value if checklist_setting else {})
        if isinstance(checklist_defs, dict):
            checklist_defs = checklist_defs.get("sections", checklist_defs)

        sap_system_code = ""
        if bg.sap_system_id:
            from bgcc.models.reference import SapSystem

            sys = db.session.get(SapSystem, bg.sap_system_id)
            sap_system_code = sys.code if sys else ""

        result = gemini_service.extract_and_checklist(
            bg_id=bg_id,
            user_id=user_id,
            pdf_bytes=pdf_bytes,
            sap_system=sap_system_code,
            bg_type=bg.bg_type,
            format_variant=bg.format_variant,
            checklist_definitions=checklist_defs,
            is_extension=bool(bg.parent_bg_id),
        )

        is_bg = bool(result.get("is_bank_guarantee", False))
        fields = {
            "bg_number": result.get("bg_number"),
            "amount": result.get("amount"),
            "currency": result.get("currency") or "INR",
            "issue_date": result.get("issue_date"),
            "expiry_date": result.get("expiry_date"),
            "claim_expiry_date": result.get("claim_expiry_date"),
            "issuing_bank": result.get("issuing_bank"),
            "issuing_bank_branch": result.get("issuing_bank_branch"),
            "vendor_name": result.get("beneficiary_name"),
            "notes": result.get("notes"),
            "field_confidences": {
                f.get("name"): {
                    "confidence": f.get("confidence"),
                    "note": f.get("note"),
                }
                for f in result.get("fields", [])
            },
        }
        checklist = result.get("checklist", [])
        clauses = result.get("clauses", [])

        analysis = _analysis_for(document)
        if analysis is None:
            analysis = DocumentAnalysis(document_id=document.id)
            db.session.add(analysis)
        analysis.classification_result = {
            "is_bank_guarantee": is_bg,
            "detected_bg_type": result.get("detected_bg_type"),
            "type_matches": result.get("type_matches"),
        }
        analysis.extracted_fields = fields
        analysis.extracted_fields["clauses"] = clauses
        analysis.checklist_result = checklist
        analysis.dispatch_readiness = intake_service.compute_dispatch_readiness(checklist)
        analysis.ai_model_version = gemini_service.model_name()
        db.session.commit()

        if not is_bg:
            bg.current_stage = "blocked_not_bg"
            db.session.commit()
            _finish_job(job)
            return {"status": "blocked_not_bg"}

        _finish_job(job)

        # Fire the parallel stage + callback chord.
        j2a = _new_job("po_sap_cross_check", bg_id, user_id)
        j2b = _new_job("template_compliance", bg_id, user_id)
        jfinal = _new_job("finalize_validation", bg_id, user_id)

        chord(
            (
                po_sap_cross_check.s(bg_id, user_id, j2a.id),
                template_compliance.s(bg_id, user_id, j2b.id),
            ),
            finalize_validation.s(bg_id=bg_id, user_id=user_id, job_id=jfinal.id),
        ).apply_async()
        return {"status": "chord_fired"}
    except Exception as exc:
        _fail_job(job, exc)
        logger.exception("bg_extraction failed for bg=%s", bg_id)
        raise self.retry(exc=exc)


@celery.task(bind=True, name="po_sap_cross_check", max_retries=2)
def po_sap_cross_check(self, bg_id, user_id, job_id=None):
    job = db.session.get(CeleryJob, job_id) if job_id else _new_job("po_sap_cross_check", bg_id, user_id)
    _start_job(job)
    bg = db.session.get(BankGuarantee, bg_id)
    try:
        document = _primary_document(bg)
        analysis = _analysis_for(document)
        fields = analysis.extracted_fields or {}
        po_context = sap_service.get_po_context(bg.po_numbers or [])
        checks = _run_po_checks(bg, fields, po_context, user_id)
        analysis.po_sap_result = {
            "po_context": po_context,
            "checks": checks,
            "shortfall": None,
        }
        # ABG shortfall stored explicitly for the UI to render unmistakably.
        if bg.bg_type == "abg":
            new_amount = intake_service.parse_money(fields.get("amount"))
            blocked, total_open = intake_service.abg_shortfall(po_context, new_amount)
            analysis.po_sap_result["shortfall"] = {
                "blocked": blocked,
                "total_open": str(total_open) if total_open is not None else None,
            }
        db.session.commit()
        _finish_job(job)
        return {"status": "completed"}
    except Exception as exc:
        _fail_job(job, exc)
        logger.exception("po_sap_cross_check failed for bg=%s", bg_id)
        raise self.retry(exc=exc)


def _run_po_checks(bg, fields, po_context, user_id):
    from datetime import date, timedelta

    from bgcc.services import gemini_service

    new_amount = intake_service.parse_money(fields.get("amount"))
    expiry = intake_service.parse_date(fields.get("expiry_date"))
    extracted_vendor = (fields.get("vendor_name") or "").strip()

    total_po = sum(
        (intake_service.parse_money(c.get("po_value")) or 0) for c in po_context
    )
    po_vendor = po_context[0].get("vendor_name", "") if po_context else ""

    checks = []

    # Amount tolerance.
    if new_amount is not None and total_po:
        if new_amount <= total_po:
            amount_status, amount_detail = "pass", f"BG amount {new_amount} is within PO value {total_po}."
        elif new_amount <= total_po * 1.10:
            amount_status, amount_detail = "warning", f"BG amount {new_amount} exceeds PO value {total_po} by up to 10%."
        else:
            amount_status, amount_detail = "fail", f"BG amount {new_amount} exceeds PO value {total_po} significantly."
    else:
        amount_status, amount_detail = "warning", "Unable to compare amount against PO value."
    checks.append(_check_entry("amount_tolerance", amount_status, amount_detail, bg, user_id,
                               {"bg_amount": str(new_amount), "po_value": str(total_po)}))

    # Expiry after minimum date.
    min_date = date.today() + timedelta(days=30)
    if expiry:
        if expiry >= min_date:
            expiry_status, expiry_detail = "pass", f"Expiry {expiry} is beyond the minimum validity date {min_date}."
        elif expiry > date.today():
            expiry_status, expiry_detail = "warning", f"Expiry {expiry} leaves less than 30 days of validity."
        else:
            expiry_status, expiry_detail = "fail", f"Expiry {expiry} is on or before today - the guarantee has already expired."
    else:
        expiry_status, expiry_detail = "warning", "Expiry date could not be read from the document."
    checks.append(_check_entry("expiry_min_date", expiry_status, expiry_detail, bg, user_id,
                               {"expiry": str(expiry)}))

    # Vendor name match.
    if not extracted_vendor or not po_vendor:
        vendor_status, vendor_detail = "warning", "Vendor name could not be fully verified."
    elif _norm(extracted_vendor) == _norm(po_vendor):
        vendor_status, vendor_detail = "pass", f"Vendor '{extracted_vendor}' matches the PO vendor."
    else:
        vendor_status, vendor_detail = _vendor_gemini_judgement(bg, user_id, extracted_vendor, po_vendor)
    checks.append(_check_entry("vendor_match", vendor_status, vendor_detail, bg, user_id,
                               {"extracted": extracted_vendor, "po": po_vendor}))

    return checks


def _norm(value):
    return "".join((value or "").lower().split())


def _vendor_gemini_judgement(bg, user_id, extracted, po_vendor):
    try:
        from bgcc.services import gemini_service

        res = gemini_service.assess_vendor_similarity(bg.id, user_id, extracted, po_vendor)
        if res.get("similar"):
            return "pass", f"'{extracted}' is a close match for PO vendor '{po_vendor}': {res.get('explanation')}"
        return "fail", f"Vendor '{extracted}' does not appear to match PO vendor '{po_vendor}': {res.get('explanation')}"
    except Exception:
        return "warning", f"Unable to fully verify vendor similarity between '{extracted}' and '{po_vendor}'."


def _check_entry(check, status, detail, bg, user_id, details):
    if status == "pass":
        return {"check": check, "status": status, "detail": detail, "explanation": None}
    explanation = _templated_explanation(check, status, detail)
    try:
        from bgcc.services import gemini_service

        res = gemini_service.generate_check_explanation(bg.id, user_id, check, details)
        explanation = res.get("explanation") or explanation
    except Exception:
        pass  # Gemini failure never blocks the deterministic result.
    return {"check": check, "status": status, "detail": detail, "explanation": explanation}


def _templated_explanation(check, status, detail):
    return (
        f"This cross-check ({check}) needs your review: {detail}. "
        "Please confirm the figures against the source document before proceeding."
    )


@celery.task(bind=True, name="template_compliance", max_retries=2)
def template_compliance(self, bg_id, user_id, job_id=None):
    job = db.session.get(CeleryJob, job_id) if job_id else _new_job("template_compliance", bg_id, user_id)
    _start_job(job)
    bg = db.session.get(BankGuarantee, bg_id)
    try:
        document = _primary_document(bg)
        analysis = _analysis_for(document)
        clauses = (analysis.extracted_fields or {}).get("clauses", [])

        is_extension = bool(bg.parent_bg_id)
        if is_extension:
            parent = db.session.get(BankGuarantee, bg.parent_bg_id)
            reference_text = "\n".join(c.get("text", "") for c in _parent_clauses(parent))
            mandatory = []
        else:
            reference_text, mandatory = _template_content()

        deviations = _chunked_comparison(bg, user_id, clauses, reference_text, mandatory, is_extension)

        # Deterministic prohibited-clause layer - final step before persisting.
        rules = _prohibited_rules()
        created = 0
        for dev in deviations:
            ai_tier = dev.get("ai_proposed_tier")
            bg_excerpt = dev.get("bg_text_excerpt")
            eff_tier, matched = effective_tier(ai_tier, bg_excerpt, rules)
            row = Deviation(
                bank_guarantee_id=bg.id,
                clause_reference=dev.get("clause_reference") or "clause",
                template_text_summary=dev.get("template_text_summary") or "",
                bg_text_excerpt=bg_excerpt,
                deviation_type=dev.get("deviation_type"),
                ai_proposed_tier=ai_tier,
                effective_tier=eff_tier,
                status="pending",
                is_missing_critical_clause=bool(dev.get("is_missing_critical_clause")),
            )
            db.session.add(row)
            created += 1
        db.session.commit()
        _finish_job(job)
        return {"status": "completed", "deviations": created}
    except Exception as exc:
        _fail_job(job, exc)
        logger.exception("template_compliance failed for bg=%s", bg_id)
        raise self.retry(exc=exc)


def _chunked_comparison(bg, user_id, clauses, reference_text, mandatory, is_extension):
    from bgcc.services import gemini_service

    if not clauses:
        return []
    all_deviations = []
    chunk_size = 6
    for i in range(0, len(clauses), chunk_size):
        chunk = clauses[i:i + chunk_size]
        pairs = [
            {
                "clause_reference": c.get("reference") or f"clause-{k + i + 1}",
                "bg_text": c.get("text", ""),
                "template_text": reference_text,
            }
            for k, c in enumerate(chunk)
        ]
        payload = {
            "chunk_index": i // chunk_size,
            "mandatory_template_clauses": mandatory,
            "chunk_pairs": pairs,
        }
        result = gemini_service.evaluate_clause_comparison(
            bg.id, user_id, payload, is_extension
        )
        all_deviations.extend(result.get("deviations", []))
    return all_deviations


@celery.task(name="finalize_validation")
def finalize_validation(results, bg_id=None, user_id=None, job_id=None):
    job = db.session.get(CeleryJob, job_id) if job_id else None
    if job is None:
        job = _new_job("finalize_validation", bg_id, user_id)
    _start_job(job)
    bg = db.session.get(BankGuarantee, bg_id)
    try:
        bg.risk_tier_summary = intake_service.compute_risk_summary(bg)
        bg.current_stage = "ready_for_review"
        bg.updated_at = datetime.utcnow()
        db.session.commit()
        _finish_job(job)
        return {"status": "ready", "bg_id": bg_id}
    except Exception as exc:
        _fail_job(job, exc)
        logger.exception("finalize_validation failed for bg=%s", bg_id)
        raise
