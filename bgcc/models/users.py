from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from bgcc.extensions import db
from bgcc.models.enums import PlatformRole


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=True)
    microsoft_oid = db.Column(db.String(128), nullable=True, unique=True)
    full_name = db.Column(db.String(255), nullable=False)
    granted_roles = db.Column(db.JSON, nullable=False, default=list)
    active_role = db.Column(db.String(50), nullable=True)
    sap_system_id = db.Column(
        db.Integer, db.ForeignKey("sap_systems.id"), nullable=True, index=True
    )
    is_approved = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_login_at = db.Column(db.DateTime, nullable=True)

    preferences = db.relationship(
        "UserPreference", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    sap_system = db.relationship("SapSystem", back_populates="users", foreign_keys=[sap_system_id])
    notifications = db.relationship(
        "Notification", back_populates="user", lazy="dynamic", cascade="all, delete-orphan"
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    @property
    def granted_role_values(self):
        return self.granted_roles or []

    @property
    def display_role(self):
        return (self.active_role or self.granted_role_values[0]).replace("_", " ").title()

    def has_granted_role(self, role):
        return role in self.granted_role_values

    @property
    def is_multi_role(self):
        return len(self.granted_role_values) > 1

    @property
    def initial(self):
        name = (self.full_name or "?").strip()
        return (name[:1] or "?").upper()


class UserPreference(db.Model):
    __tablename__ = "user_preferences"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False, index=True)
    language = db.Column(db.String(5), nullable=False, default="en")
    notify_email = db.Column(db.Boolean, nullable=False, default=True)
    notify_in_app = db.Column(db.Boolean, nullable=False, default=True)
    notify_push = db.Column(db.Boolean, nullable=False, default=False)
    date_format = db.Column(db.String(20), nullable=False, default="%d %b %Y")
    push_subscription = db.Column(db.JSON, nullable=True)

    user = db.relationship("User", back_populates="preferences")
