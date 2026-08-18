import os
import secrets

import click
from flask.cli import with_appcontext

from bgcc.extensions import db
from bgcc.models.enums import PlatformRole
from bgcc.models.reference import SapSystem
from bgcc.models.sap_reference import SapPoRecord
from bgcc.models.settings import ApplicationSetting
from bgcc.models.users import User, UserPreference
from bgcc.services import audit_service
from bgcc.services.notification_service import dispatch

from bgcc.services.seed_service import (
    DEFAULT_SAP_SYSTEMS,
    LOCAL_PO_RECORDS,
    STARTER_SETTINGS,
    seed_admin_user,
    seed_purchase_orders as run_seed_purchase_orders,
    seed_sap_systems,
    seed_starter_settings,
)


@click.command("seed-purchase-orders", help="Seed the local PO reference dataset for the SAP cross-check fallback.")
@with_appcontext
def seed_purchase_orders():
    run_seed_purchase_orders(echo=click.echo)


@click.group("users", help="Manage platform users.")
@with_appcontext
def users_group():
    """User management commands."""


@click.command("seed-dev-data", help="Bootstrap reference data for a fresh development environment.")
@with_appcontext
def seed_dev_data():
    seed_sap_systems(echo=click.echo)
    seed_admin_user(echo=click.echo)
    seed_starter_settings(echo=click.echo)
    run_seed_purchase_orders(echo=click.echo)
    click.echo("Seed: complete. You can sign in as the admin account.")


@click.command("approve", help="Approve a pending registration and assign roles and business-unit scope.")
@click.argument("email")
@click.option("--roles", required=True, help="Comma-separated role values to grant.")
@click.option("--sap-system", default=None, help="SAP system code for the user's business-unit scope.")
@with_appcontext
def approve(email, roles, sap_system):
    user = User.query.filter_by(email=email.strip().lower()).first()
    if not user:
        raise click.ClickException(f"No user with email {email}.")
    role_values = [r.strip() for r in roles.split(",") if r.strip()]
    valid = {member.value for member in PlatformRole}
    unknown = [r for r in role_values if r not in valid]
    if unknown:
        raise click.ClickException(f"Unknown role(s): {', '.join(unknown)}")

    if sap_system:
        system = SapSystem.query.filter_by(code=sap_system).first()
        if not system:
            raise click.ClickException(f"No SAP system with code {sap_system}.")
        user.sap_system_id = system.id
    elif user.sap_system_id is None:
        default = SapSystem.query.filter_by(code=os.environ.get("DEFAULT_SAP_SYSTEM", "GRP001")).first()
        if default:
            user.sap_system_id = default.id

    user.granted_roles = role_values
    if user.active_role not in role_values:
        user.active_role = role_values[0] if len(role_values) == 1 else None
    user.is_approved = True
    db.session.commit()

    dispatch(
        user_id=user.id,
        notification_type="account_approved",
        title="Your access has been approved",
        body="Welcome to BG Command Centre. You can now sign in and start working.",
        link_url="/",
        email_to=user.email,
        email_subject="Your BG Command Centre access has been approved",
        email_body=(
            f"Hi {user.full_name},\n\nYour BG Command Centre account has been approved. "
            "You can now sign in and start working.\n\nBest regards,\nBG Command Centre"
        ),
        triggered_by=user.id,
    )
    audit_service.record(
        "account_approved",
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        metadata_json={"email": user.email, "roles": role_values},
    )
    click.echo(f"Approved {user.email} with roles {', '.join(role_values)}.")


@click.command("create-admin", help="Create or promote an account to the admin role.")
@click.argument("email", required=False, default=None)
@with_appcontext
def create_admin(email):
    email = (email or os.environ.get("SEED_ADMIN_EMAIL") or "admin@bg.center").lower()
    user = User.query.filter_by(email=email).first()
    password = os.environ.get("SEED_ADMIN_PASSWORD")
    if not user:
        if not password:
            password = secrets.token_urlsafe(12)
            click.echo(f"Generated admin password -> {password} (set SEED_ADMIN_PASSWORD to avoid this)")
        user = User(
            email=email,
            full_name=os.environ.get("SEED_ADMIN_NAME") or "Platform Administrator",
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


users_group.add_command(approve)
users_group.add_command(create_admin)


def register_cli(app):
    app.cli.add_command(seed_dev_data)
    app.cli.add_command(seed_purchase_orders)
    app.cli.add_command(users_group)

