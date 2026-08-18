from enum import Enum

from bgcc.extensions import db


def enum_col(enum_cls, default=None, nullable=False, **kwargs):
    return db.Column(
        db.Enum(
            enum_cls,
            values_callable=lambda e: [member.value for member in e],
            native_enum=False,
            length=64,
        ),
        nullable=nullable,
        default=default,
        **kwargs,
    )


class PlatformRole(str, Enum):
    creator = "creator"
    buyer = "buyer"
    tc_head = "tc_head"
    bu_fc = "bu_fc"
    bu_cfmc = "bu_cfmc"
    coordinator = "coordinator"
    ceo_cfo = "ceo_cfo"
    abex = "abex"
    admin = "admin"

    @classmethod
    def choices(cls):
        return [(member.value, member.name.replace("_", " ").title()) for member in cls]


class ExpenditureType(str, Enum):
    capex = "capex"
    opex = "opex"


class BGType(str, Enum):
    pbg = "pbg"
    abg = "abg"
    cpbg = "cpbg"
    cpbg_cum_pbg = "cpbg_cum_pbg"
    cg = "cg"


class FormatVariant(str, Enum):
    supply = "supply"
    service = "service"


class BGStatus(str, Enum):
    draft = "draft"
    pending_buyer_approval = "pending_buyer_approval"
    pending_category_lead_approval = "pending_category_lead_approval"
    pending_fc_approval = "pending_fc_approval"
    pending_bu_cfmc_approval = "pending_bu_cfmc_approval"
    pending_ceo_cfo = "pending_ceo_cfo"
    pending_abex_verification = "pending_abex_verification"
    submitted_to_bank = "submitted_to_bank"
    live = "live"
    rejected = "rejected"
    closed = "closed"


class DeviationTier(str, Enum):
    low = "low"
    high = "high"
    prohibited = "prohibited"


class DeviationStatus(str, Enum):
    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"


class JobStatus(str, Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class WorkflowAction(str, Enum):
    approve_forward = "approve_forward"
    reject = "reject"
    submit = "submit"
    verify = "verify"
    hold = "hold"
    close = "close"
    extension_requested = "extension_requested"
    extension_uploaded = "extension_uploaded"
    closure_initiated = "closure_initiated"
    closure_reviewed = "closure_reviewed"
    closure_rejected = "closure_rejected"
    executive_approved = "executive_approved"
    executive_declined = "executive_declined"
    closure_verified = "closure_verified"
    return_requested = "return_requested"
    return_dispatched = "return_dispatched"
    return_receipt_confirmed = "return_receipt_confirmed"
    invocation_draft_generated = "invocation_draft_generated"
    invocation_signed_uploaded = "invocation_signed_uploaded"
    invocation_sent_to_bank = "invocation_sent_to_bank"
    invocation_hold_requested = "invocation_hold_requested"
    invocation_hold_released = "invocation_hold_released"
