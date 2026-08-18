from datetime import datetime

from bgcc.extensions import db


class GeneratedDocument(db.Model):
    __tablename__ = "generated_documents"

    id = db.Column(db.Integer, primary_key=True)
    bank_guarantee_id = db.Column(
        db.Integer, db.ForeignKey("bank_guarantees.id"), nullable=True, index=True
    )
    document_kind = db.Column(db.String(50), nullable=False)
    storage_path = db.Column(db.String(500), nullable=False)
    file_format = db.Column(db.String(10), nullable=False, default="docx")
    generated_by_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True, index=True
    )
    source_ai_interaction_id = db.Column(
        db.Integer, db.ForeignKey("ai_interactions.id"), nullable=True, index=True
    )
    version = db.Column(db.Integer, nullable=False, default=1)
    generated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
