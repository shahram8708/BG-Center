from datetime import datetime

from bgcc.extensions import db


class ExtensionRequest(db.Model):
    __tablename__ = "extension_requests"

    id = db.Column(db.Integer, primary_key=True)
    parent_bg_id = db.Column(
        db.Integer, db.ForeignKey("bank_guarantees.id"), nullable=False, index=True
    )
    stage = db.Column(db.String(20), nullable=False, default="not_started")
    vendor_email = db.Column(db.String(255), nullable=True)
    requested_at = db.Column(db.DateTime, nullable=True)
    is_overdue = db.Column(db.Boolean, nullable=False, default=False)
    child_bg_id = db.Column(
        db.Integer, db.ForeignKey("bank_guarantees.id"), nullable=True, index=True
    )
    coordinator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class BgClosure(db.Model):
    __tablename__ = "bg_closures"

    id = db.Column(db.Integer, primary_key=True)
    bank_guarantee_id = db.Column(
        db.Integer, db.ForeignKey("bank_guarantees.id"), nullable=False, index=True
    )
    is_exception = db.Column(db.Boolean, nullable=False, default=False)
    eligibility_reasoning = db.Column(db.Text, nullable=False)
    exception_justification = db.Column(db.Text, nullable=True)
    stage = db.Column(db.String(40), nullable=False, default="initiated")
    initiated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    verified_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    closed_at = db.Column(db.DateTime, nullable=True)
    # Executive magic-link approval (Step 4). Tokens are hashed at rest.
    cfo_approval_token = db.Column(db.String(128), nullable=True)
    cfo_approved_at = db.Column(db.DateTime, nullable=True)
    ceo_approval_token = db.Column(db.String(128), nullable=True)
    ceo_approved_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class BgReturn(db.Model):
    __tablename__ = "bg_returns"

    id = db.Column(db.Integer, primary_key=True)
    bank_guarantee_id = db.Column(
        db.Integer, db.ForeignKey("bank_guarantees.id"), nullable=False, index=True
    )
    dispatch_id = db.Column(db.Integer, db.ForeignKey("dispatches.id"), nullable=True, index=True)
    status = db.Column(db.String(30), nullable=False, default="requested")
    requested_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    receipt_confirmed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    receipt_confirmed_at = db.Column(db.DateTime, nullable=True)


class BgInvocation(db.Model):
    __tablename__ = "bg_invocations"

    id = db.Column(db.Integer, primary_key=True)
    bank_guarantee_id = db.Column(
        db.Integer, db.ForeignKey("bank_guarantees.id"), unique=True, nullable=False, index=True
    )
    stage = db.Column(db.String(30), nullable=False, default="approaching_window")
    draft_document_id = db.Column(
        db.Integer, db.ForeignKey("generated_documents.id"), nullable=True, index=True
    )
    signed_document_id = db.Column(
        db.Integer, db.ForeignKey("documents.id"), nullable=True, index=True
    )
    ceo_approval_token = db.Column(db.String(255), nullable=True)
    ceo_approved_at = db.Column(db.DateTime, nullable=True)
    sent_to_bank_at = db.Column(db.DateTime, nullable=True)
    hold_requested_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    hold_cfo_approval_token = db.Column(db.String(128), nullable=True)
    hold_cfo_approved_at = db.Column(db.DateTime, nullable=True)
    hold_ceo_approval_token = db.Column(db.String(128), nullable=True)
    hold_ceo_approved_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
