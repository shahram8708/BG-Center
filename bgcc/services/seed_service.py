import contextlib
import logging
import os
import time

from bgcc.extensions import db
from bgcc.models.enums import PlatformRole
from bgcc.models.reference import SapSystem
from bgcc.models.sap_reference import SapPoRecord
from bgcc.models.settings import ApplicationSetting
from bgcc.models.users import User, UserPreference

logger = logging.getLogger(__name__)

LOCAL_PO_RECORDS = [
    {"po_number": "PO-2026-1001", "vendor_name": "Tata Steel Limited", "po_value": "2500000", "open_advance_amount": "900000", "is_executed": True},
    {"po_number": "PO-2026-1002", "vendor_name": "Tata Steel Limited", "po_value": "1800000", "open_advance_amount": None, "is_executed": True},
    {"po_number": "PO-2026-2001", "vendor_name": "Larsen & Toubro Ltd", "po_value": "9500000", "open_advance_amount": "4200000", "is_executed": False},
    {"po_number": "PO-2026-2002", "vendor_name": "Larsen & Toubro Ltd", "po_value": "3200000", "open_advance_amount": None, "is_executed": True},
    {"po_number": "PO-2026-3001", "vendor_name": "Reliance Industries Ltd", "po_value": "5600000", "open_advance_amount": "1200000", "is_executed": True},
    {"po_number": "PO-2026-4001", "vendor_name": "Infosys Limited", "po_value": "4100000", "open_advance_amount": None, "is_executed": True},
    {"po_number": "PO-2026-5001", "vendor_name": "Adani Ports Ltd", "po_value": "7800000", "open_advance_amount": "2500000", "is_executed": False},
    {"po_number": "PO-2026-6001", "vendor_name": "Bharat Heavy Electricals", "po_value": "6350000", "open_advance_amount": None, "is_executed": True},
]

DEFAULT_SAP_SYSTEMS = [
    {"code": "GRP001", "display_name": "Group Corporate", "business_unit": "Corporate", "sap_connection_type": "oauth"},
    {"code": "INFRA001", "display_name": "Infrastructure", "business_unit": "Infrastructure & EPC", "sap_connection_type": "oauth"},
    {"code": "MFG001", "display_name": "Manufacturing", "business_unit": "Manufacturing", "sap_connection_type": "basic_auth"},
    {"code": "SRV001", "display_name": "Services", "business_unit": "Shared Services", "sap_connection_type": "basic_auth"},
]

STARTER_SETTINGS = {
    "doa_matrix": {
        "description": "Data-driven delegation-of-authority matrix. Stage order and deviation visibility are read from here at runtime and can be edited in the admin UI.",
        "stage_sequence": {
            "opex": ["buyer", "tc_head", "bu_fc", "ceo_cfo", "abex"],
            "capex": ["buyer", "tc_head", "bu_cfmc", "ceo_cfo", "abex"],
        },
        "ceo_cfo_condition": {
            "on_tiers": ["high", "prohibited"],
        },
        "deviation_visibility": {
            "buyer": ["low", "high", "prohibited"],
            "tc_head": ["low", "high", "prohibited"],
            "bu_fc": ["low", "high", "prohibited"],
            "bu_cfmc": ["low", "high", "prohibited"],
            "ceo_cfo": ["low", "high", "prohibited"],
            "abex": ["low", "high", "prohibited"],
        },
    },
    "active_clause_template": {
        "name": "Baseline Supply & Service BG Clause Template",
        "variant": "supply",
        "body": (
            "WHEREAS the Vendor has agreed to supply goods / render services under the referenced Purchase Order, "
            "and WHEREAS a Bank Guarantee is required as security for the due performance of the Vendor's obligations, "
            "NOW THEREFORE, in consideration of the premises, the Bank unconditionally and irrevocably guarantees and "
            "undertakes to pay the Beneficiary on first written demand any sum up to the guaranteed amount, without "
            "demur or protest. This guarantee shall remain valid until the expiry date and shall be returned to the "
            "Bank upon its expiry or upon discharge of the underlying obligations."
        ),
        "mandatory_clauses": [
            "beneficiary_identity",
            "amount_and_currency",
            "validity_period",
            "first_demand_payable",
            "unconditional_and_irrevocable",
            "return_on_expiry",
        ],
    },
    "prohibited_clause_patterns": [
        {"pattern": r"(?i)\bunlimited\s+liability\b", "reason": "Unlimited liability is not permitted."},
        {"pattern": r"(?i)\barbitration\s+outside\s+india\b", "reason": "Arbitration must be seated in India."},
        {"pattern": r"(?i)\bgoverning\s+law[^.]*\bforeign\b", "reason": "Governing law must be Indian law."},
    ],
    "checklist_definitions": {
        "sections": [
            {"key": "header", "label": "Header & Parties", "items": ["beneficiary_identity", "amount_and_currency", "guarantee_type"]},
            {"key": "body", "label": "Body & Obligations", "items": ["unconditional_and_irrevocable", "first_demand_payable", "validity_period"]},
            {"key": "closing", "label": "Closing & Return", "items": ["return_on_expiry", "claim_window", "signature_authority"]},
        ]
    },
    "approved_banks": {
        "note": "Starter list of commonly used issuing banks. Manage via the admin editor.",
        "banks": [
            {"name": "State Bank of India", "short_code": "SBI", "contact_email": "bgclaims@sbi.co.in"},
            {"name": "HDFC Bank", "short_code": "HDFC", "contact_email": "claims@hdfcbank.com"},
            {"name": "ICICI Bank", "short_code": "ICICI", "contact_email": "claims@icicibank.com"},
            {"name": "Punjab National Bank", "short_code": "PNB", "contact_email": ""},
        ],
    },
    "extension_policy": {
        "warning_days": 45,
        "overdue_days": 21,
    },
    "executive_contacts": {
        "cfo_email": "cfo@bg.center",
        "ceo_email": "ceo@bg.center",
    },
    "executive_approval_expiry_hours": 72,
    "invocation_policy": {
        "approaching_days": 60,
        "critical_days": 14,
    },
    "bank_verification_expiry_hours": 48,
    "policy_reference_content": {
        "sections": [
            {
                "title": "Deviation risk tiers",
                "body": (
                    "Clause deviations are classified into three risk tiers. Low-tier deviations "
                    "are minor wording differences that do not change the guarantee's substance; "
                    "they require standard review. High-tier deviations are material changes that "
                    "need a reviewer's explicit attention. A Prohibited-tier deviation can never be "
                    "accepted by any approver at any workflow step, and it can never be downgraded by "
                    "an approver — only a platform administrator can clear a prohibited-tier block. "
                    "This is a deliberate, non-overridable safeguard."
                ),
                "link": "/bg-multi-stage-approval",
            },
            {
                "title": "Delegation of authority routing",
                "body": (
                    "Every Bank Guarantee moves through a fixed delegation-of-authority chain. For "
                    "an OPEX expenditure the Finance stage is signed off by the BU FC; for a CAPEX "
                    "expenditure it is signed off by the BU CFMC. When a BG's highest risk tier is "
                    "High or Prohibited, the workflow also routes through an elevated CEO/CFO "
                    "sign-off before ABEX verification. Approvers see a BG only at the stage they "
                    "are authorized to act on."
                ),
                "link": "/bg-multi-stage-approval",
            },
            {
                "title": "Extension policy window",
                "body": (
                    "Live Bank Guarantees are monitored daily against expiry. When a guarantee comes "
                    "within the warning window (45 days), the platform flags it and lets a Coordinator "
                    "request an extension from the vendor. When it crosses the overdue window (21 "
                    "days) with no resolution, the item is marked overdue so it is never missed. "
                    "Extended guarantees are uploaded and validated through the standard pipeline."
                ),
                "link": "/bg-extension",
            },
            {
                "title": "Invocation claim window",
                "body": (
                    "A claim (invocation) can be made at any point in a guarantee's life, but the "
                    "platform proactively monitors the claim window so it is never missed near the "
                    "deadline. When a Live BG's claim-expiry date is within 60 days it is flagged "
                    "approaching, and within 14 days it is marked critical. A claim is drafted, "
                    "signed, CEO-approved via a secure link, and automatically sent to the bank "
                    "only once both the signed letter and CEO approval are present."
                ),
                "link": "/bg-invocation",
            },
            {
                "title": "Bank Guarantee status meanings",
                "body": (
                    "A Bank Guarantee passes through these statuses: draft (created but not "
                    "submitted), the pending_* approval stages (awaiting a specific role), "
                    "submitted_to_bank (the claim or guarantee was sent to the issuing bank), live "
                    "(active and in force), rejected (a submission that was declined and is "
                    "terminal), and closed (wound down after closure approval)."
                ),
                "link": "/bg-status",
            },
            {
                "title": "Closure eligibility",
                "body": (
                    "A Bank Guarantee can be closed only when its underlying purchase order or "
                    "contract is fully executed, and for an advance guarantee the open advance is "
                    "zero. Any guarantee not meeting this is an exception and must go through "
                    "category-lead review and sequential CFO/CEO executive approval before ABEX "
                    "verification. Eligibility is always computed live from financial records, never "
                    "chosen manually."
                ),
                "link": "/bg-closure",
            },
            {
                "title": "Bank verification",
                "body": (
                    "When a Bank Guarantee goes Live, the platform emails the issuing bank a secure "
                    "link asking it to confirm the guarantee is authentic. This is an anti-fraud "
                    "check. If the bank does not respond within the configured window, the item is "
                    "marked no-response and a Coordinator is reminded. A Coordinator can resend the "
                    "request or apply a manual override with a reference note when the bank responds "
                    "outside the digital link."
                ),
                "link": "/bg-bank-tracker",
            },
        ]
    },
}


@contextlib.contextmanager
def _seed_lock(lock_dir=None, timeout=10.0, poll_interval=0.05):
    if lock_dir is None:
        lock_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "uploads"))
    os.makedirs(lock_dir, exist_ok=True)
    lock_file = os.path.join(lock_dir, ".seed.lock")
    start = time.time()
    fd = None
    while True:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            break
        except FileExistsError:
            try:
                stat = os.stat(lock_file)
                if time.time() - stat.st_mtime > 60:
                    try:
                        os.remove(lock_file)
                    except OSError:
                        pass
            except OSError:
                pass
            if time.time() - start >= timeout:
                break
            time.sleep(poll_interval)
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.remove(lock_file)
            except OSError:
                pass


def seed_sap_systems(echo=None):
    created = []
    for spec in DEFAULT_SAP_SYSTEMS:
        try:
            with db.session.begin_nested():
                if not SapSystem.query.filter_by(code=spec["code"]).first():
                    db.session.add(SapSystem(**spec))
                    created.append(spec["code"])
        except Exception:
            db.session.rollback()
    db.session.commit()
    if echo and created:
        echo(f"Seed: SAP systems ready ({', '.join(created)}).")
    return created


def seed_admin_user(echo=None):
    admin_email = (os.environ.get("SEED_ADMIN_EMAIL") or "admin@bg.center").lower()
    created = False
    admin = User.query.filter_by(email=admin_email).first()
    if not admin:
        default_sap = SapSystem.query.filter_by(code="GRP001").first()
        admin = User(
            email=admin_email,
            full_name=os.environ.get("SEED_ADMIN_NAME") or "Platform Administrator",
            granted_roles=[PlatformRole.admin.value],
            active_role=PlatformRole.admin.value,
            is_approved=True,
            is_active=True,
            sap_system_id=default_sap.id if default_sap else None,
        )
        password = os.environ.get("SEED_ADMIN_PASSWORD") or os.environ.get("DEFAULT_ADMIN_PASSWORD", "Admin@12345")
        admin.set_password(password)
        try:
            with db.session.begin_nested():
                db.session.add(admin)
                db.session.flush()
                db.session.add(UserPreference(user_id=admin.id))
            db.session.commit()
            created = True
            if echo:
                echo(f"Seed: approved admin account ready -> {admin_email}")
        except Exception:
            db.session.rollback()
    else:
        pref = UserPreference.query.filter_by(user_id=admin.id).first()
        if not pref:
            try:
                with db.session.begin_nested():
                    db.session.add(UserPreference(user_id=admin.id))
                db.session.commit()
            except Exception:
                db.session.rollback()
    return created


def seed_starter_settings(echo=None):
    created = []
    for key, value in STARTER_SETTINGS.items():
        try:
            with db.session.begin_nested():
                existing = ApplicationSetting.query.filter_by(setting_key=key).first()
                if not existing:
                    db.session.add(ApplicationSetting(setting_key=key, setting_value=value, change_reason="Initial seed"))
                    created.append(key)
                elif key == "doa_matrix" and "stage_sequence" not in (existing.setting_value or {}):
                    existing.setting_value = value
                    existing.version = (existing.version or 1) + 1
                    existing.change_reason = "Seed upgrade to structured DoA matrix"
        except Exception:
            db.session.rollback()
    db.session.commit()
    if echo and created:
        echo("Seed: starter application_settings populated.")
    return created


def seed_purchase_orders(echo=None):
    created = 0
    for spec in LOCAL_PO_RECORDS:
        try:
            with db.session.begin_nested():
                if not SapPoRecord.query.filter_by(po_number=spec["po_number"]).first():
                    db.session.add(SapPoRecord(
                        po_number=spec["po_number"],
                        vendor_name=spec["vendor_name"],
                        po_value=spec["po_value"],
                        open_advance_amount=spec["open_advance_amount"],
                        is_executed=spec.get("is_executed", False),
                    ))
                    created += 1
        except Exception:
            db.session.rollback()
    db.session.commit()
    if echo and created:
        echo(f"Seed: local PO records ready ({created} created, else already present).")
    return created


def initialize_seed_data(app=None, echo=None):
    if os.environ.get("SKIP_AUTO_SEED"):
        return

    log = (app.logger if app else None) or logger

    try:
        log.info("Seed initialization started")
        db.create_all()

        upload_dir = app.config.get("UPLOAD_FOLDER") if app else None
        with _seed_lock(lock_dir=upload_dir):
            seed_sap_systems(echo=echo)
            seed_admin_user(echo=echo)
            seed_starter_settings(echo=echo)
            seed_purchase_orders(echo=echo)
            from bgcc.services.demo_seed_service import seed_demo_data
            seed_demo_data(echo=echo)

        log.info("Seed initialization completed")
    except Exception as e:
        log.error(f"Seed initialization failed: {e}", exc_info=True)
        try:
            db.session.rollback()
        except Exception:
            pass
