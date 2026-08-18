"""File-on-disk helpers used by intake and (later) document lifecycle features."""
import os

from bgcc.extensions import db
from bgcc.models.documents import Document


def document_path(doc):
    if not doc or not doc.storage_path:
        return None
    if os.path.isabs(doc.storage_path):
        return doc.storage_path
    from flask import current_app

    return os.path.join(current_app.config["UPLOAD_FOLDER"], doc.storage_path)


def delete_document_files(doc):
    """Best-effort removal of a document's bytes from disk."""
    path = document_path(doc)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def resolve_document(doc_id):
    return db.session.get(Document, int(doc_id)) if doc_id else None
