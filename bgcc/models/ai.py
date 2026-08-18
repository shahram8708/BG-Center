from datetime import datetime

from bgcc.extensions import db


class AiInteraction(db.Model):
    __tablename__ = "ai_interactions"

    id = db.Column(db.Integer, primary_key=True)
    feature = db.Column(db.String(64), nullable=False)
    related_bg_id = db.Column(
        db.Integer, db.ForeignKey("bank_guarantees.id"), nullable=True, index=True
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    model_version = db.Column(db.String(64), nullable=True)
    prompt_token_count = db.Column(db.Integer, nullable=True)
    response_token_count = db.Column(db.Integer, nullable=True)
    latency_ms = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="success")
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
