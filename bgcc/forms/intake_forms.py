from flask_wtf import FlaskForm
from wtforms import (
    DateField,
    EmailField,
    SelectField,
    SelectMultipleField,
    StringField,
)
from wtforms.validators import DataRequired, Length, Optional

from bgcc.models.enums import BGType, ExpenditureType, FormatVariant


def _enum_choices(enum_cls):
    return [(member.value, member.name.replace("_", " ").title()) for member in enum_cls]


class NewBgDetailsForm(FlaskForm):
    sap_system_id = SelectField(
        "SAP system / business unit",
        coerce=int,
        validators=[DataRequired(message="Select a SAP system.")],
    )
    bg_type = SelectField(
        "Bank Guarantee type",
        choices=_enum_choices(BGType),
        validators=[DataRequired(message="Select a BG type.")],
    )
    format_variant = SelectField(
        "Format variant",
        choices=_enum_choices(FormatVariant),
        validators=[DataRequired(message="Select a format variant.")],
    )
    expenditure_type = SelectField(
        "Expenditure type",
        choices=_enum_choices(ExpenditureType),
        default="capex",
        validators=[DataRequired(message="Select an expenditure type.")],
    )


class ExtendedBgDetailsForm(FlaskForm):
    parent_bg_id = SelectField(
        "Parent Bank Guarantee",
        coerce=int,
        validators=[DataRequired(message="Select a parent Bank Guarantee.")],
    )
    issue_date = DateField(
        "New issue date (from the extended BG)",
        validators=[DataRequired(message="Enter the new issue date.")],
    )
    expiry_date = DateField(
        "New expiry date (from the extended BG)",
        validators=[DataRequired(message="Enter the new expiry date.")],
    )


class IntakeReviewForm(FlaskForm):
    # Edited / confirmed extracted fields (pre-filled from AI extraction).
    bg_number = StringField("BG number", validators=[Optional(), Length(max=64)])
    amount = StringField("Amount", validators=[DataRequired(message="Enter the amount.")])
    currency = SelectField(
        "Currency",
        choices=[("INR", "INR"), ("USD", "USD"), ("EUR", "EUR"), ("GBP", "GBP")],
        default="INR",
        validators=[DataRequired()],
    )
    issue_date = DateField("Issue date", validators=[DataRequired()])
    expiry_date = DateField("Expiry date", validators=[DataRequired()])
    claim_expiry_date = DateField("Claim expiry date", validators=[Optional()])
    issuing_bank = StringField("Issuing bank", validators=[Optional(), Length(max=255)])
    vendor_name = StringField("Vendor / beneficiary", validators=[Optional(), Length(max=255)])

    # Missing-critical-clause acknowledgements.
    acknowledgements = SelectMultipleField("Acknowledged critical clauses", coerce=int)

    # Human-confirmed extracted field names (marking them as reviewed).
    confirmed_fields = SelectMultipleField("Confirmed fields", coerce=str)

    # Dispatch details.
    dispatch_mode = SelectField(
        "Dispatch mode",
        choices=[("", "Select dispatch mode"), ("courier", "Courier"), ("cmr", "CMR (handover)")],
        validators=[Optional()],
    )
    courier_name = StringField("Courier name", validators=[Optional(), Length(max=120)])
    tracking_number = StringField("Tracking number", validators=[Optional(), Length(max=120)])
    cmr_deliverer_name = StringField("Deliverer name", validators=[Optional(), Length(max=120)])
    cmr_deliverer_email = EmailField("Deliverer email", validators=[Optional()])
    cmr_deliverer_mobile = StringField("Deliverer mobile", validators=[Optional(), Length(max=40)])
