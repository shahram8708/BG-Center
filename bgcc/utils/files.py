import os
import uuid

from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "png", "jpg", "jpeg", "xlsx", "xls"}


def safe_filename(original_filename):
    base = secure_filename(original_filename or "")
    name, ext = os.path.splitext(base)
    return f"{uuid.uuid4().hex}{ext}"


def storage_path(folder, original_filename):
    name = safe_filename(original_filename)
    return os.path.join(folder, name)


def is_allowed_file(filename, allowed=None):
    allowed = allowed or ALLOWED_EXTENSIONS
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed


def display_filename(original_filename):
    """Sanitized original filename for display only (never a storage path)."""
    return secure_filename(original_filename or "") or "document.pdf"
