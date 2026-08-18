from datetime import datetime

from bgcc.extensions import db
from bgcc.models.enums import enum_col, DeviationStatus, DeviationTier


class Deviation(db.Model):
    __tablename__ = "deviations"

    id = db.Column(db.Integer, primary_key=True)
    bank_guarantee_id = db.Column(
        db.Integer, db.ForeignKey("bank_guarantees.id"), nullable=False, index=True
    )
    clause_reference = db.Column(db.String(120), nullable=False)
    template_text_summary = db.Column(db.Text, nullable=False)
    bg_text_excerpt = db.Column(db.Text, nullable=True)
    deviation_type = db.Column(db.String(80), nullable=True)
    ai_proposed_tier = enum_col(DeviationTier, nullable=True)
    effective_tier = enum_col(DeviationTier, nullable=True)
    status = enum_col(DeviationStatus, default=DeviationStatus.pending, nullable=False)
    decided_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    decided_at = db.Column(db.DateTime, nullable=True)
    decision_comment = db.Column(db.Text, nullable=True)
    tier_changed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    is_missing_critical_clause = db.Column(db.Boolean, nullable=False, default=False)
    # Prohibited-clause administrator override (Step 7). effective_tier is never
    # changed; the override is recorded as a separate, fully visible fact.
    admin_override_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    admin_override_at = db.Column(db.DateTime, nullable=True)
    admin_override_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
