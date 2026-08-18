from datetime import datetime

from bgcc.extensions import db


class SapPoRecord(db.Model):
    """Local reference dataset backing the PO/SAP cross-check fallback.

    Populated with the organization's financial records. A real SAP endpoint is
    preferred when configured; this table is the clearly-labeled local fallback.
    """

    __tablename__ = "sap_po_records"

    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(64), unique=True, nullable=False, index=True)
    vendor_name = db.Column(db.String(255), nullable=False)
    po_value = db.Column(db.Numeric(18, 2), nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="INR")
    open_advance_amount = db.Column(db.Numeric(18, 2), nullable=True)
    is_executed = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
