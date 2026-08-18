from datetime import datetime

from bgcc.extensions import db


class Dispatch(db.Model):
    __tablename__ = "dispatches"

    id = db.Column(db.Integer, primary_key=True)
    bank_guarantee_id = db.Column(
        db.Integer, db.ForeignKey("bank_guarantees.id"), nullable=False, index=True
    )
    context_type = db.Column(db.String(20), nullable=False)
    dispatch_mode = db.Column(db.String(20), nullable=False)
    courier_name = db.Column(db.String(120), nullable=True)
    tracking_number = db.Column(db.String(120), nullable=True)
    cmr_deliverer_name = db.Column(db.String(120), nullable=True)
    cmr_deliverer_email = db.Column(db.String(255), nullable=True)
    cmr_deliverer_mobile = db.Column(db.String(40), nullable=True)
    dispatched_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    dispatched_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
