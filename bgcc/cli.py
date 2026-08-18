import os
import secrets

import click
from flask.cli import with_appcontext

from bgcc.extensions import db
from bgcc.models.enums import PlatformRole
from bgcc.models.users import User, UserPreference
from bgcc.services import audit_service


@click.command("create-admin", help="Create or promote an account to the admin role.")
@click.argument("email", required=False, default=None)
@with_appcontext
def create_admin(email):
    email = (email or os.environ.get("ADMIN_EMAIL") or "admin@bg.center").lower()
    user = User.query.filter_by(email=email).first()
    password = os.environ.get("ADMIN_PASSWORD")
    if not user:
        if not password:
            password = secrets.token_urlsafe(12)
            click.echo(f"Generated admin password -> {password} (set ADMIN_PASSWORD to avoid this)")
        user = User(
            email=email,
            full_name=os.environ.get("ADMIN_NAME") or "Platform Administrator",
            granted_roles=[PlatformRole.admin.value],
            active_role=PlatformRole.admin.value,
            is_approved=True,
            is_active=True,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        db.session.add(UserPreference(user_id=user.id))
        db.session.commit()
        click.echo(f"Created admin {email}.")
    else:
        user.granted_roles = list(dict.fromkeys((user.granted_roles or []) + [PlatformRole.admin.value]))
        user.active_role = PlatformRole.admin.value
        user.is_approved = True
        user.is_active = True
        if password:
            user.set_password(password)
        db.session.commit()
        click.echo(f"Promoted {email} to admin.")
    audit_service.record(
        "account_approved",
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        metadata_json={"email": email, "roles": [PlatformRole.admin.value], "via": "cli"},
    )


def register_cli(app):
    app.cli.add_command(create_admin)
