"""Bank-side authenticity verification (Step 6).

Automatically triggered when a BG goes Live: creates a `bank_verifications`
row and dispatches a magic-link email (via Step 4's generic service) to the
issuing bank's contact asking them to confirm the guarantee is authentic. A
30-minute poll reviews pending verifications; Coordinators can resend or apply
a manual override when the bank responds outside the digital link.
"""
from datetime import datetime

from bgcc.extensions import db
from bgcc.models.bank_verifications import BankVerification
from bgcc.models.reference import BankGuarantee
from bgcc.services import audit_service, magic_link_service


def _bank_contact_email(bg):
    from bgcc.models.settings import ApplicationSetting

    setting = ApplicationSetting.query.filter_by(setting_key="approved_banks").first()
    value = (setting.setting_value if setting else {}) or {}
    banks = value.get("banks", []) if isinstance(value, dict) else value or []
    for bank in banks:
        name = bank.get("name", "")
        if name and bg.issuing_bank and (
            name.lower() in bg.issuing_bank.lower() or bg.issuing_bank.lower() in name.lower()
        ):
            return bank.get("contact_email")
    return None


def _on_confirm(record):
    record.status = "confirmed"
    record.confirmed_at = datetime.utcnow()
    db.session.commit()
    bg = db.session.get(BankGuarantee, record.bank_guarantee_id)
    audit_service.record(
        "bank_verification_confirmed", target_type="bank_verification", target_id=record.id,
        metadata_json={"bg_number": bg.bg_number if bg else None},
    )


def _on_dispute(record):
    record.status = "disputed"
    db.session.commit()
    bg = db.session.get(BankGuarantee, record.bank_guarantee_id)
    audit_service.record(
        "bank_verification_disputed", target_type="bank_verification", target_id=record.id,
        metadata_json={"bg_number": bg.bg_number if bg else None},
    )


def _register_target():
    if _register_target.done:
        return
    magic_link_service.register_target(
        key="bank_verification",
        model=BankVerification,
        token_attr="verification_token",
        ts_attr="confirmed_at",
        salt="bank-verification",
        max_age_settings_key="bank_verification_expiry_hours",
        max_age_default=48 * 3600,
        title="Bank Guarantee authenticity verification",
        subject="Please confirm a Bank Guarantee issued by your bank",
        on_approve=_on_confirm,
        on_decline=_on_dispute,
    )
    _register_target.done = True


_register_target.done = False
_register_target()


def trigger_verification(bg):
    """Create/refresh a bank_verifications row and dispatch the request email."""
    _register_target()
    verification = BankVerification.query.filter_by(bank_guarantee_id=bg.id).first()
    if verification is None:
        verification = BankVerification(bank_guarantee_id=bg.id, status="not_sent")
        db.session.add(verification)
        db.session.flush()
    verification.bank_contact_email = _bank_contact_email(bg)
    verification.status = "pending"
    db.session.commit()

    email = verification.bank_contact_email
    if not email:
        # No bank contact configured - leave as not_sent rather than fake-send.
        verification.status = "not_sent"
        verification.sent_at = None
        db.session.commit()
        audit_service.record(
            "bank_verification_no_contact", target_type="bank_verification", target_id=verification.id,
            metadata_json={"bg_number": bg.bg_number},
        )
        return verification

    magic_link_service.issue_and_email(
        "bank_verification", verification, email,
        extra_lines=[
            f"Bank Guarantee: {bg.bg_number}",
            f"Vendor: {bg.vendor_name or 'n/a'}",
            f"Amount: {bg.amount} {bg.currency}",
            "Please confirm the authenticity of this guarantee via the secure link.",
        ],
    )
    verification.sent_at = datetime.utcnow()
    db.session.commit()
    audit_service.record(
        "bank_verification_sent", target_type="bank_verification", target_id=verification.id,
        metadata_json={"bg_number": bg.bg_number, "email": email},
    )
    return verification


def resend_verification(bg):
    return trigger_verification(bg)


def manual_set_status(verification, status, reference, user):
    if status not in ("confirmed", "disputed"):
        raise ValueError("Invalid manual verification status.")
    if not (reference or "").strip():
        raise ValueError("A reference note is required for a manual override.")
    verification.status = status
    verification.response_reference = reference
    if status == "confirmed":
        verification.confirmed_at = datetime.utcnow()
    db.session.commit()
    bg = db.session.get(BankGuarantee, verification.bank_guarantee_id)
    audit_service.record(
        "bank_verification_manual_override", actor_id=user.id,
        target_type="bank_verification", target_id=verification.id,
        metadata_json={"bg_number": bg.bg_number if bg else None, "status": status,
                       "reference": reference},
    )
    return verification


def verification_for(bg):
    return BankVerification.query.filter_by(bank_guarantee_id=bg.id).first()


def poll_pending(self):
    """Review every pending verification; expire tokens with no response."""
    from bgcc.models.settings import ApplicationSetting

    setting = ApplicationSetting.query.filter_by(
        setting_key="bank_verification_expiry_hours"
    ).first()
    expiry_hours = int(setting.setting_value) if setting and setting.setting_value is not None else 48
    now = datetime.utcnow()
    expired = 0
    pending = BankVerification.query.filter_by(status="pending").all()
    for verification in pending:
        verification.last_polled_at = now
        if verification.sent_at is None:
            # Never actually sent (e.g. no contact); treat as no_response.
            verification.status = "no_response"
            expired += 1
            db.session.commit()
            continue
        age = (now - verification.sent_at).total_seconds() / 3600
        if age > expiry_hours and verification.verification_token is not None:
            verification.status = "no_response"
            expired += 1
            _notify_coordinator(verification)
        db.session.commit()
    return {"expired": expired}


def _notify_coordinator(verification):
    from bgcc.models.users import User
    from bgcc.services.notification_service import dispatch
    from bgcc.utils.urls import build_absolute_url

    bg = db.session.get(BankGuarantee, verification.bank_guarantee_id)
    holders = User.query.filter(
        User.is_approved.is_(True), User.is_active.is_(True),
    ).all()
    detail_url = build_absolute_url(f"/bg/{bg.id}") if bg else build_absolute_url("/")
    for holder in holders:
        if "coordinator" in (holder.granted_roles or []):
            dispatch(
                user_id=holder.id, notification_type="bank_verification_no_response",
                title="Bank verification awaiting response",
                body=f"The bank has not responded to the verification request for {bg.bg_number if bg else 'a BG'}.",
                link_url=detail_url, email_to=holder.email,
                email_subject="Bank verification awaiting response",
                email_body=f"The bank has not responded to the verification request for {bg.bg_number if bg else 'a BG'}.\n\nDirect link: {detail_url}",
                template_name="emails/notification.html",
                template_context={
                    "details": {
                        "Guarantee Number": bg.bg_number if bg else "N/A",
                        "Bank": bg.issuing_bank if bg else "N/A",
                    },
                    "action_text": "View Guarantee Details",
                    "action_url": detail_url,
                    "link_url": detail_url,
                },
            )
