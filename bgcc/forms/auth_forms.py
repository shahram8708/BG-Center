from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    EmailField,
    PasswordField,
    SelectField,
    SelectMultipleField,
    StringField,
)
from wtforms.validators import (
    DataRequired,
    Email,
    EqualTo,
    Length,
    Optional,
)

from bgcc.models.enums import PlatformRole
from bgcc.utils.validators import CompanyEmail, validate_password_complexity


def _registrable_role_choices():
    return [
        (member.value, member.name.replace("_", " ").title())
        for member in PlatformRole
        if member is not PlatformRole.admin
    ]


class SignInForm(FlaskForm):
    email = EmailField(
        "Work email",
        validators=[DataRequired(message="Enter your work email."), Email()],
    )
    password = PasswordField(
        "Password", validators=[DataRequired(message="Enter your password.")]
    )
    remember = BooleanField("Keep me signed in", default=False)


class RegisterForm(FlaskForm):
    full_name = StringField(
        "Full name",
        validators=[DataRequired(message="Enter your full name."), Length(max=255)],
    )
    email = EmailField(
        "Work email",
        validators=[
            DataRequired(message="Enter your work email."),
            Email(message="Enter a valid email address."),
            CompanyEmail(),
        ],
    )
    sap_system_id = SelectField(
        "SAP system / business unit",
        coerce=int,
        validators=[DataRequired(message="Select your SAP system / business unit.")],
    )
    roles = SelectMultipleField(
        "Requested role(s)",
        choices=_registrable_role_choices(),
        validators=[DataRequired(message="Select at least one role.")],
    )
    manager_email = EmailField(
        "Manager's email (for approval context)",
        validators=[Optional(), Email()],
    )
    password = PasswordField(
        "Password",
        validators=[
            DataRequired(message="Create a password."),
            validate_password_complexity,
        ],
    )
    confirm = PasswordField(
        "Confirm password",
        validators=[
            DataRequired(message="Confirm your password."),
            EqualTo("password", message="Passwords do not match."),
        ],
    )


class ForgotPasswordForm(FlaskForm):
    email = EmailField(
        "Work email",
        validators=[DataRequired(message="Enter your work email."), Email()],
    )


class ResetPasswordForm(FlaskForm):
    password = PasswordField(
        "New password",
        validators=[
            DataRequired(message="Enter a new password."),
            validate_password_complexity,
        ],
    )
    confirm = PasswordField(
        "Confirm new password",
        validators=[
            DataRequired(message="Confirm your new password."),
            EqualTo("password", message="Passwords do not match."),
        ],
    )


class RoleSelectForm(FlaskForm):
    role = SelectField(
        "Select active role", coerce=str, validators=[DataRequired(message="Choose a role.")]
    )
