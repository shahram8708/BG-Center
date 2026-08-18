from datetime import datetime

from bgcc.extensions import db
from bgcc.models.enums import enum_col, BGType, ExpenditureType, FormatVariant, BGStatus


class SapSystem(db.Model):
    __tablename__ = "sap_systems"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(255), nullable=False)
    business_unit = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    sap_connection_type = db.Column(db.String(20), nullable=False, default="oauth")
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    users = db.relationship("User", back_populates="sap_system", foreign_keys="User.sap_system_id")


class BankGuarantee(db.Model):
    __tablename__ = "bank_guarantees"
    __table_args__ = (
        db.Index("ix_bg_expenditure", "expenditure_type"),
        db.Index("ix_bg_expiry", "expiry_date"),
        db.Index("ix_bg_claim_expiry", "claim_expiry_date"),
        db.Index("ix_bg_vendor", "vendor_name"),
        db.Index("ix_bg_status", "status"),
        db.Index("ix_bg_stage", "current_stage"),
    )

    id = db.Column(db.Integer, primary_key=True)
    bg_number = db.Column(db.String(64), unique=True, nullable=False, index=True)
    parent_bg_id = db.Column(db.Integer, db.ForeignKey("bank_guarantees.id"), nullable=True, index=True)
    bg_type = enum_col(BGType, nullable=False)
    format_variant = enum_col(FormatVariant, nullable=False)
    expenditure_type = enum_col(ExpenditureType, nullable=False)
    sap_system_id = db.Column(db.Integer, db.ForeignKey("sap_systems.id"), nullable=True, index=True)
    amount = db.Column(db.Numeric(18, 2), nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="INR")
    issue_date = db.Column(db.Date, nullable=False)
    expiry_date = db.Column(db.Date, nullable=False)
    claim_expiry_date = db.Column(db.Date, nullable=True)
    issuing_bank = db.Column(db.String(255), nullable=True)
    vendor_name = db.Column(db.String(255), nullable=True)
    po_numbers = db.Column(db.JSON, nullable=False, default=list)
    status = enum_col(BGStatus, default=BGStatus.draft, nullable=False)
    current_stage = db.Column(db.String(64), nullable=True)
    saved_as_draft = db.Column(db.Boolean, nullable=False, default=False)
    risk_tier_summary = db.Column(db.Text, nullable=True)
    creator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    coordinator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    parent_bg = db.relationship(
        "BankGuarantee", remote_side=[id], backref="extension_children", foreign_keys=[parent_bg_id]
    )
