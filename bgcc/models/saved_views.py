from datetime import datetime

from bgcc.extensions import db


class SavedView(db.Model):
    __tablename__ = "saved_views"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    page_key = db.Column(db.String(64), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    filter_state = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
