from datetime import datetime

from bgcc.extensions import db
from bgcc.models.enums import enum_col, JobStatus


class CeleryJob(db.Model):
    __tablename__ = "celery_jobs"

    id = db.Column(db.Integer, primary_key=True)
    celery_task_id = db.Column(db.String(64), unique=True, nullable=True)
    task_name = db.Column(db.String(120), nullable=False)
    status = enum_col(JobStatus, default=JobStatus.queued, nullable=False)
    related_bg_id = db.Column(
        db.Integer, db.ForeignKey("bank_guarantees.id"), nullable=True, index=True
    )
    triggered_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    result_ref = db.Column(db.String(255), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    retry_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
