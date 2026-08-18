from datetime import datetime

from bgcc.extensions import db
from bgcc.models.enums import enum_col
from bgcc.models.enums import WorkflowAction


class WorkflowHistory(db.Model):
    __tablename__ = "workflow_history"

    id = db.Column(db.Integer, primary_key=True)
    bank_guarantee_id = db.Column(
        db.Integer, db.ForeignKey("bank_guarantees.id"), nullable=False, index=True
    )
    from_stage = db.Column(db.String(64), nullable=True)
    to_stage = db.Column(db.String(64), nullable=False)
    action = enum_col(WorkflowAction, nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    actor_role = db.Column(db.String(50), nullable=True)
    comments = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
