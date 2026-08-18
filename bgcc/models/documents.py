from datetime import datetime

from bgcc.extensions import db


class Document(db.Model):
    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    bank_guarantee_id = db.Column(
        db.Integer, db.ForeignKey("bank_guarantees.id"), nullable=True, index=True
    )
    document_type = db.Column(db.String(50), nullable=False)
    storage_path = db.Column(db.String(500), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(120), nullable=True)
    file_size_bytes = db.Column(db.Integer, nullable=False, default=0)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class DocumentAnalysis(db.Model):
    __tablename__ = "document_analyses"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(
        db.Integer, db.ForeignKey("documents.id"), unique=True, nullable=False, index=True
    )
    classification_result = db.Column(db.JSON, nullable=False, default=dict)
    extracted_fields = db.Column(db.JSON, nullable=False, default=dict)
    po_sap_result = db.Column(db.JSON, nullable=True)
    checklist_result = db.Column(db.JSON, nullable=True)
    dispatch_readiness = db.Column(db.String(20), nullable=True)
    ai_model_version = db.Column(db.String(64), nullable=True)
    processing_duration_ms = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
