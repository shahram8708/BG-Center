from datetime import datetime

from bgcc.extensions import db


class BankVerification(db.Model):
    __tablename__ = "bank_verifications"

    id = db.Column(db.Integer, primary_key=True)
    bank_guarantee_id = db.Column(
        db.Integer, db.ForeignKey("bank_guarantees.id"), unique=True, nullable=False, index=True
    )
    status = db.Column(db.String(20), nullable=False, default="not_sent")
    sent_at = db.Column(db.DateTime, nullable=True)
    confirmed_at = db.Column(db.DateTime, nullable=True)
    bank_contact_email = db.Column(db.String(255), nullable=True)
    response_reference = db.Column(db.Text, nullable=True)
    last_polled_at = db.Column(db.DateTime, nullable=True)
    # Token hash for the bank-verification magic link (hashed at rest).
    verification_token = db.Column(db.String(128), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
