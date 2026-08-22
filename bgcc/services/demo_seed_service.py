"""Production-realistic demo/staging seed data generator for BG Command Centre.

This module is ADDITIVE to `bgcc/services/seed_service.py`: it never recreates
or duplicates the baseline reference data (`SapSystem`, the default admin
`User`, `ApplicationSetting`, `SapPoRecord`) that `initialize_seed_data()`
already guarantees on every app boot. It looks that data up by its known
natural keys (`code`, `email`, `po_number`) and builds on top of it.

Entry point: `seed_demo_data(echo=None, reset=False)`, wired to the Flask CLI
as `flask seed-demo-data` (see `bgcc/cli.py`).

Design notes (see the accompanying SEED_DATA_PLAN.md for the full write-up):
  * Every timestamp is computed relative to `datetime.utcnow()` at run time
    (module-level `NOW`, set inside `seed_demo_data()`), never hardcoded.
  * Historical `AuditLog` / `Notification` / `CeleryJob` rows are constructed
    directly via the ORM, never through `audit_service.record()` or
    `notification_service.dispatch()` -- those two hardcode `utcnow()` on the
    row they create and `dispatch()` additionally enqueues a real Celery send,
    which would both defeat backdating and fire needless side effects if
    called in a 100+ BG backfill loop.
  * Idempotency: every unique-constrained natural key (`email`, `po_number`,
    `bg_number`) is looked up before insert; the whole run is safe to execute
    more than once. A `reset=True` flag additionally wipes only the rows this
    module previously created (tracked by an `ApplicationSetting` marker),
    never the baseline seed data.
"""
import logging
import random
from datetime import datetime, timedelta
from decimal import Decimal

from bgcc.extensions import db
from bgcc.models.enums import WorkflowAction
from bgcc.models.users import User, UserPreference
from bgcc.models.reference import SapSystem, BankGuarantee
from bgcc.models.sap_reference import SapPoRecord
from bgcc.models.documents import Document, DocumentAnalysis
from bgcc.models.deviations import Deviation
from bgcc.models.generated_documents import GeneratedDocument
from bgcc.models.workflow import WorkflowHistory
from bgcc.models.dispatches import Dispatch
from bgcc.models.lifecycle import ExtensionRequest, BgClosure, BgReturn, BgInvocation
from bgcc.models.ai import AiInteraction
from bgcc.models.jobs import CeleryJob
from bgcc.models.saved_views import SavedView
from bgcc.models.audit import AuditLog
from bgcc.models.notifications import Notification
from bgcc.models.settings import ApplicationSetting
from bgcc.models.bank_verifications import BankVerification
from bgcc.models.assistant_messages import AssistantMessage

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEMO_PASSWORD = "Seed@2026!"          # satisfies validate_password_complexity
COMPANY_DOMAIN = "bg.center"          # matches Config.COMPANY_EMAIL_DOMAIN default
MARKER_SETTING_KEY = "demo_seed_marker"   # idempotency / reset tracking marker

NOW = None    # set by seed_demo_data(); every timestamp derives from this
rng = random.Random(20260601)   # fixed seed -> reproducible-but-realistic runs


def days_ago(n, hour=None, minute=0):
    """A datetime N days before NOW, optionally pinned to a specific hour."""
    dt = NOW - timedelta(days=n)
    if hour is not None:
        dt = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt > NOW:
            dt = dt - timedelta(days=1)
    return dt


def hours_ago(n):
    return NOW - timedelta(hours=n)


def minutes_ago(n):
    return NOW - timedelta(minutes=n)


class Clock:
    """Per-BG internal chronology helper.

    Guarantees every subsequent timestamp is strictly >= the previous one
    (monotonically increasing) and never exceeds NOW, while still feeling
    like a believable dwell time per lifecycle step (Section 6).
    """

    def __init__(self, start):
        self.t = start

    def advance(self, minutes=0, hours=0, days=0):
        base = timedelta(days=days, hours=hours, minutes=minutes)
        if base.total_seconds() > 0:
            base = base * rng.uniform(0.55, 1.6)
        nxt = self.t + base
        if nxt <= self.t:
            nxt = self.t + timedelta(minutes=1)
        if nxt > NOW:
            nxt = NOW
        self.t = nxt
        return self.t

    def now(self):
        return self.t


def _fake_token():
    """A plausible-looking hashed magic-link token for a *currently pending*
    (not yet consumed) executive/bank approval -- see the token-lifecycle
    note in the plan: a real token exists from the moment the email is
    dispatched until the instant it's consumed, at which point the real app
    clears it to NULL (Section 14.2). We only ever show a non-null token on
    rows that are still awaiting a response for that same reason."""
    return "".join(rng.choice("0123456789abcdef") for _ in range(64))


def pick(seq):
    return rng.choice(seq)


def maybe(prob):
    return rng.random() < prob


def weighted_pick(pairs):
    """pairs: list of (value, weight)."""
    total = sum(w for _, w in pairs)
    r = rng.uniform(0, total)
    upto = 0
    for value, w in pairs:
        upto += w
        if upto >= r:
            return value
    return pairs[-1][0]


# --------------------------------------------------------------------------
# Counters printed in the final summary
# --------------------------------------------------------------------------

COUNTS = {}


def _bump(model_name, n=1):
    COUNTS[model_name] = COUNTS.get(model_name, 0) + n


# --------------------------------------------------------------------------
# Persona roster (Section 10) -- 28 new users spanning every role, archetype,
# and SAP system, plus the 1 baseline admin already seeded by seed_service.py.
# --------------------------------------------------------------------------
# archetype in {power, regular, casual, new, dormant, admin_staff, rejected}

PERSONAS = [
    # ---- GRP001 (Group Corporate) ----
    {"name": "Priya Sharma", "email": "priya.sharma", "roles": ["buyer", "coordinator"], "sap": "GRP001", "archetype": "power"},
    {"name": "Arjun Mehta", "email": "arjun.mehta", "roles": ["tc_head"], "sap": "GRP001", "archetype": "power"},
    {"name": "Rohan Kapoor", "email": "rohan.kapoor", "roles": ["bu_fc"], "sap": "GRP001", "archetype": "regular"},
    {"name": "Ananya Iyer", "email": "ananya.iyer", "roles": ["bu_cfmc"], "sap": "GRP001", "archetype": "regular"},
    {"name": "Vikram Nair", "email": "vikram.nair", "roles": ["abex"], "sap": "GRP001", "archetype": "regular"},
    {"name": "Sneha Reddy", "email": "sneha.reddy", "roles": ["creator", "coordinator"], "sap": "GRP001", "archetype": "regular"},
    # ---- INFRA001 (Infrastructure & EPC) ----
    {"name": "Karan Malhotra", "email": "karan.malhotra", "roles": ["buyer"], "sap": "INFRA001", "archetype": "regular"},
    {"name": "Divya Krishnan", "email": "divya.krishnan", "roles": ["tc_head"], "sap": "INFRA001", "archetype": "regular"},
    {"name": "Aditya Rao", "email": "aditya.rao", "roles": ["bu_fc"], "sap": "INFRA001", "archetype": "regular"},
    {"name": "Neha Gupta", "email": "neha.gupta", "roles": ["bu_cfmc"], "sap": "INFRA001", "archetype": "casual"},
    {"name": "Rahul Verma", "email": "rahul.verma", "roles": ["abex"], "sap": "INFRA001", "archetype": "power"},
    {"name": "Ishita Bose", "email": "ishita.bose", "roles": ["creator", "coordinator"], "sap": "INFRA001", "archetype": "regular"},
    # ---- MFG001 (Manufacturing) ----
    {"name": "Siddharth Chatterjee", "email": "siddharth.chatterjee", "roles": ["buyer"], "sap": "MFG001", "archetype": "casual"},
    {"name": "Pooja Desai", "email": "pooja.desai", "roles": ["tc_head"], "sap": "MFG001", "archetype": "regular"},
    {"name": "Aman Joshi", "email": "aman.joshi", "roles": ["bu_fc"], "sap": "MFG001", "archetype": "regular"},
    {"name": "Kavya Menon", "email": "kavya.menon", "roles": ["bu_cfmc"], "sap": "MFG001", "archetype": "casual"},
    {"name": "Manoj Pillai", "email": "manoj.pillai", "roles": ["abex"], "sap": "MFG001", "archetype": "casual"},
    {"name": "Trisha Bhatt", "email": "trisha.bhatt", "roles": ["creator", "coordinator"], "sap": "MFG001", "archetype": "regular"},
    # ---- SRV001 (Shared Services) ----
    {"name": "Varun Chopra", "email": "varun.chopra", "roles": ["buyer"], "sap": "SRV001", "archetype": "new"},
    {"name": "Meera Iyengar", "email": "meera.iyengar", "roles": ["tc_head"], "sap": "SRV001", "archetype": "casual"},
    {"name": "Nikhil Saxena", "email": "nikhil.saxena", "roles": ["bu_fc"], "sap": "SRV001", "archetype": "regular"},
    {"name": "Ritu Agarwal", "email": "ritu.agarwal", "roles": ["bu_cfmc"], "sap": "SRV001", "archetype": "new"},
    {"name": "Devika Ramanathan", "email": "devika.ramanathan", "roles": ["abex"], "sap": "SRV001", "archetype": "dormant"},
    {"name": "Farhan Ali", "email": "farhan.ali", "roles": ["creator", "coordinator"], "sap": "SRV001", "archetype": "regular"},
    # ---- Cross-cutting ----
    {"name": "Alexander Whitfield", "email": "alexander.whitfield", "roles": ["ceo_cfo"], "sap": "GRP001", "archetype": "casual"},
    {"name": "Kabir Malhotra", "email": "kabir.malhotra", "roles": ["admin"], "sap": "GRP001", "archetype": "admin_staff"},
    {"name": "Ayesha Siddiqui", "email": "ayesha.siddiqui", "roles": ["creator"], "sap": "MFG001", "archetype": "new_unapproved"},
    {"name": "Gaurav Oberoi", "email": "gaurav.oberoi", "roles": ["buyer"], "sap": "SRV001", "archetype": "rejected"},
]

# Archetype -> (created_days_ago range, number of login sessions across the
# window, last_login recency in days, is_approved, is_active)
ARCHETYPE_PROFILE = {
    "power":          {"created_range": (75, 90), "sessions": (18, 26), "last_login_days": (0, 1)},
    "regular":        {"created_range": (60, 88), "sessions": (9, 15), "last_login_days": (0, 3)},
    "casual":         {"created_range": (55, 85), "sessions": (3, 6), "last_login_days": (2, 8)},
    "new":            {"created_range": (1, 7), "sessions": (1, 3), "last_login_days": (0, 2)},
    "dormant":        {"created_range": (80, 90), "sessions": (4, 8), "last_login_days": (21, 40)},
    "admin_staff":    {"created_range": (80, 90), "sessions": (16, 24), "last_login_days": (0, 1)},
    "new_unapproved": {"created_range": (1, 4), "sessions": (0, 0), "last_login_days": (999, 999)},
    "rejected":       {"created_range": (30, 45), "sessions": (1, 2), "last_login_days": (29, 44)},
}


# --------------------------------------------------------------------------
# Vendors / Purchase Orders (Section 7, 14.6) -- new POs added on top of the
# 8 already seeded by seed_service.py. Same register: large Indian
# industrial / EPC / infrastructure / PSU names.
# --------------------------------------------------------------------------

NEW_PO_RECORDS = [
    {"po_number": "PO-2026-7001", "vendor_name": "Tata Projects Limited", "po_value": "5400000", "open_advance_amount": "1600000", "is_executed": False},
    {"po_number": "PO-2026-7002", "vendor_name": "Tata Projects Limited", "po_value": "2100000", "open_advance_amount": None, "is_executed": True},
    {"po_number": "PO-2026-8001", "vendor_name": "Shapoorji Pallonji and Company", "po_value": "8800000", "open_advance_amount": "3000000", "is_executed": False},
    {"po_number": "PO-2026-9001", "vendor_name": "Hindustan Construction Company", "po_value": "4650000", "open_advance_amount": None, "is_executed": True},
    {"po_number": "PO-2026-9002", "vendor_name": "Hindustan Construction Company", "po_value": "3300000", "open_advance_amount": "500000", "is_executed": True},
    {"po_number": "PO-2026-10001", "vendor_name": "GMR Infrastructure Limited", "po_value": "6200000", "open_advance_amount": None, "is_executed": True},
    {"po_number": "PO-2026-11001", "vendor_name": "NTPC Limited", "po_value": "9100000", "open_advance_amount": "2200000", "is_executed": False},
    {"po_number": "PO-2026-12001", "vendor_name": "Power Grid Corporation of India", "po_value": "3950000", "open_advance_amount": None, "is_executed": True},
    {"po_number": "PO-2026-13001", "vendor_name": "Siemens Limited India", "po_value": "2750000", "open_advance_amount": None, "is_executed": True},
    {"po_number": "PO-2026-13002", "vendor_name": "Siemens Limited India", "po_value": "1900000", "open_advance_amount": "450000", "is_executed": True},
    {"po_number": "PO-2026-14001", "vendor_name": "ABB India Limited", "po_value": "3100000", "open_advance_amount": None, "is_executed": True},
    {"po_number": "PO-2026-15001", "vendor_name": "JSW Steel Limited", "po_value": "7400000", "open_advance_amount": "1800000", "is_executed": False},
    {"po_number": "PO-2026-16001", "vendor_name": "Vedanta Limited", "po_value": "5850000", "open_advance_amount": None, "is_executed": True},
    {"po_number": "PO-2026-17001", "vendor_name": "UltraTech Cement Limited", "po_value": "4400000", "open_advance_amount": None, "is_executed": True},
]

# Banks -- keep strictly to what's in the seeded `approved_banks` setting
# (Section 14.5): resend/verification lookups match by substring, so
# inventing an issuing_bank string outside this list would silently fall
# back to a not_sent/no_contact edge case for that BG.
ISSUING_BANKS = ["State Bank of India", "HDFC Bank", "ICICI Bank", "Punjab National Bank"]

BG_TYPES = ["pbg", "abg", "cpbg", "cpbg_cum_pbg", "cg"]
FORMAT_VARIANTS = ["supply", "service"]

# Clause reference library, aligned with the seeded active_clause_template's
# mandatory_clauses and checklist_definitions (Section 7) so deviation text
# reads like a real cross-check against this app's own template, not filler.
CLAUSE_LIBRARY = [
    ("beneficiary_identity", "Beneficiary name and address must exactly match the company's registered legal entity name."),
    ("amount_and_currency", "The guaranteed amount and currency must be stated unambiguously and match the PO value."),
    ("validity_period", "The guarantee must state a fixed validity period with an explicit expiry date."),
    ("first_demand_payable", "The bank must undertake to pay on first written demand, without demur or protest."),
    ("unconditional_and_irrevocable", "The guarantee must be unconditional and irrevocable for its full validity period."),
    ("return_on_expiry", "The original guarantee must be returned to the bank upon expiry or discharge of obligations."),
    ("guarantee_type", "The guarantee type (PBG / ABG / CPBG) must match what was requested at intake."),
    ("claim_window", "The claim window following expiry must be clearly stated and consistent with policy."),
    ("signature_authority", "The guarantee must be signed by an authorized signatory with a verifiable signing authority."),
]

# Deviation-type labels used to vary `deviation_type` realistically.
DEVIATION_TYPES = ["wording_variance", "missing_clause", "amount_mismatch", "date_format_variance", "jurisdiction_variance", "signatory_variance"]

PROHIBITED_EXCERPTS = [
    ("Notwithstanding the above, the Bank's liability under this guarantee shall be unlimited liability and shall not be capped at the guaranteed amount.", "Unlimited liability is not permitted."),
    ("Any dispute arising under this guarantee shall be referred to arbitration outside India, seated in Singapore.", "Arbitration must be seated in India."),
    ("This guarantee shall be governed by and construed in accordance with the governing law of a foreign jurisdiction, namely the laws of England and Wales.", "Governing law must be Indian law."),
]


# --------------------------------------------------------------------------
# 1. Users + preferences  (Section 4 #1, #2 ; Section 10)
# --------------------------------------------------------------------------

def seed_users(echo=None):
    """Create the 28-persona roster on top of the existing seeded admin.

    Idempotent by email. Returns a dict: {"by_email": {...}, "by_role_sap":
    {(role, sap_code): [User, ...]}, "all": [User, ...], "admin": User}.
    """
    sap_by_code = {s.code: s for s in SapSystem.query.all()}
    created = 0
    users_by_email = {}

    for spec in PERSONAS:
        email = f"{spec['email']}@{COMPANY_DOMAIN}"
        existing = User.query.filter_by(email=email).first()
        if existing:
            users_by_email[email] = existing
            continue
        profile = ARCHETYPE_PROFILE[spec["archetype"]]
        created_at = days_ago(rng.randint(*profile["created_range"]), hour=rng.randint(8, 18))
        sap = sap_by_code.get(spec["sap"])
        is_unapproved = spec["archetype"] == "new_unapproved"
        is_rejected = spec["archetype"] == "rejected"

        # Real registration leaves active_role=None for multi-role signups
        # until their first role_select, but these accounts represent 60-90
        # days of aged history, so by now they would long since have chosen
        # a primary working role -- pick the first granted role deterministically.
        user = User(
            email=email,
            full_name=spec["name"],
            granted_roles=list(spec["roles"]),
            active_role=spec["roles"][0],
            sap_system_id=sap.id if sap else None,
            is_approved=not (is_unapproved or is_rejected),
            is_active=not is_rejected,
            created_at=created_at,
        )
        user.set_password(DEMO_PASSWORD)

        if profile["last_login_days"][0] >= 999:
            user.last_login_at = None
        else:
            user.last_login_at = hours_ago(rng.randint(
                profile["last_login_days"][0] * 24 + 1,
                max(profile["last_login_days"][1] * 24 + 6, profile["last_login_days"][0] * 24 + 2),
            ))

        db.session.add(user)
        db.session.flush()

        pref = UserPreference(
            user_id=user.id,
            language=("hi" if maybe(0.12) else "en"),
            notify_email=True,
            notify_in_app=True,
            notify_push=maybe(0.2),
            date_format="%d %b %Y",
        )
        db.session.add(pref)

        # Matching AuditLog trail for how this account actually came to be.
        db.session.add(AuditLog(
            event_type="registration_submitted", actor_id=user.id, target_type="user",
            target_id=str(user.id), metadata_json={"email": email, "roles": spec["roles"]},
            created_at=created_at,
        ))
        if is_rejected:
            reviewed_at = created_at + timedelta(days=rng.randint(1, 3))
            admin = users_by_email.get(f"kabir.malhotra@{COMPANY_DOMAIN}")
            db.session.add(AuditLog(
                event_type="registration_rejected",
                actor_id=(admin.id if admin else None), target_type="user",
                target_id=str(user.id), metadata_json={"email": email},
                created_at=min(reviewed_at, NOW),
            ))
        elif not is_unapproved:
            approved_at = created_at + timedelta(hours=rng.randint(2, 30))
            admin = users_by_email.get(f"admin@{COMPANY_DOMAIN}")
            db.session.add(AuditLog(
                event_type="account_approved",
                actor_id=(admin.id if admin else None), target_type="user",
                target_id=str(user.id),
                metadata_json={"email": email, "roles": spec["roles"], "sap_system_id": sap.id if sap else None},
                created_at=min(approved_at, NOW),
            ))
            db.session.add(Notification(
                user_id=user.id, notification_type="account_approved",
                title="Your access has been approved",
                body="Welcome to BG Command Centre. You can now sign in and start working.",
                link_url="/", is_read=True,
                created_at=min(approved_at, NOW), read_at=min(approved_at + timedelta(hours=1), NOW),
            ))
            _bump("Notification")
        _bump("AuditLog", 2 if not is_unapproved else 1)

        users_by_email[email] = user
        created += 1
        _bump("User")
        _bump("UserPreference")

    db.session.commit()

    all_users = list(users_by_email.values())
    by_role_sap = {}
    for u in all_users:
        if not u.sap_system_id:
            continue
        sys = SapSystem.query.get(u.sap_system_id)
        code = sys.code if sys else None
        for r in (u.granted_roles or []):
            by_role_sap.setdefault((r, code), []).append(u)

    if echo:
        echo(f"Seed: users ready ({created} created, {len(all_users)} total incl. admin).")

    return {
        "by_email": users_by_email,
        "by_role_sap": by_role_sap,
        "all": all_users,
        "admin": users_by_email.get(f"admin@{COMPANY_DOMAIN}"),
    }


# --------------------------------------------------------------------------
# 2. Purchase orders  (Section 4 #21)
# --------------------------------------------------------------------------

def seed_more_purchase_orders(echo=None):
    created = 0
    for spec in NEW_PO_RECORDS:
        if SapPoRecord.query.filter_by(po_number=spec["po_number"]).first():
            continue
        db.session.add(SapPoRecord(
            po_number=spec["po_number"], vendor_name=spec["vendor_name"],
            po_value=spec["po_value"], open_advance_amount=spec["open_advance_amount"],
            is_executed=spec.get("is_executed", False),
        ))
        created += 1
        _bump("SapPoRecord")
    db.session.commit()
    if echo and created:
        echo(f"Seed: additional PO records ready ({created} created).")

    # Vendor -> [SapPoRecord, ...] map covering the full 22-PO catalogue.
    vendor_map = {}
    for rec in SapPoRecord.query.all():
        vendor_map.setdefault(rec.vendor_name, []).append(rec)
    return vendor_map


# --------------------------------------------------------------------------
# 3. Notification helper (recency-skewed sampling -- Section 5's "skewed
#    toward the last 7 days" instruction resolves the tension between a
#    comprehensive AuditLog and a deliberately-curated Notification volume)
# --------------------------------------------------------------------------

def _notify(user, notification_type, title, body, link_url, at_time, triggered_by_id=None):
    """Create a Notification + its CeleryJob(notification.send), sampled by
    recency so Notification stays a believable, inbox-sized subset of every
    real event rather than a 1:1 mirror of the full AuditLog trail."""
    if user is None:
        return
    age_days = (NOW - at_time).total_seconds() / 86400.0
    if age_days <= 14:
        keep = True
    else:
        keep = maybe(0.32)
    if not keep:
        return
    is_read = maybe(0.75) if age_days > 1 else maybe(0.35)
    read_at = None
    if is_read:
        read_at = at_time + timedelta(hours=rng.uniform(0.2, min(48, max(1, age_days * 24))))
        if read_at > NOW:
            read_at = NOW
    notif = Notification(
        user_id=user.id, notification_type=notification_type, title=title, body=body,
        link_url=link_url, is_read=is_read, created_at=at_time, read_at=read_at,
    )
    db.session.add(notif)
    _bump("Notification")
    db.session.add(CeleryJob(
        task_name="notification.send", status="completed", triggered_by=triggered_by_id,
        created_at=at_time, completed_at=at_time + timedelta(seconds=rng.randint(1, 20)),
    ))
    _bump("CeleryJob")


def _role_holders(users_by_role_sap, role, sap_code):
    return users_by_role_sap.get((role, sap_code), [])


def _first_holder(users_by_role_sap, role, sap_code):
    holders = _role_holders(users_by_role_sap, role, sap_code)
    return holders[0] if holders else None


# --------------------------------------------------------------------------
# 4. Deviations, checklist, risk summary  (Section 4 #7, 14.1)
# --------------------------------------------------------------------------

def _create_deviations(bg, sap_code, clock, decided_by_users, requires_ceo_cfo, count):
    """Create `count` Deviation rows for `bg`. If requires_ceo_cfo, at least
    one deviation is forced to high/prohibited tier; otherwise every tier
    stays low. A small share of "requires_ceo_cfo" BGs get a prohibited-tier
    deviation, most of which are then admin-overridden so the BG can still
    progress (Section 14.1)."""
    clauses = rng.sample(CLAUSE_LIBRARY, k=min(count, len(CLAUSE_LIBRARY)))
    rows = []
    forced_high_done = False
    for i, (clause_ref, template_summary) in enumerate(clauses):
        is_missing = maybe(0.12)
        excerpt = None
        ai_tier = "low"
        deviation_type = pick(DEVIATION_TYPES)

        if requires_ceo_cfo and not forced_high_done and i == 0:
            # Force at least one high/prohibited deviation so this BG's
            # highest effective_tier genuinely requires elevated sign-off.
            if maybe(0.28):
                ai_tier = "prohibited"
                bg_excerpt, _reason = pick(PROHIBITED_EXCERPTS)
                excerpt = bg_excerpt
            else:
                ai_tier = "high"
                excerpt = (
                    f"The Vendor's obligations under clause '{clause_ref}' shall be read down to the extent "
                    "permitted by applicable law, subject to a materially different notice period than the template."
                )
            forced_high_done = True
        elif not requires_ceo_cfo:
            ai_tier = "low"
            excerpt = f"Clause '{clause_ref}' differs from the template only in minor wording; substance is unchanged."
        else:
            ai_tier = weighted_pick([("low", 0.75), ("high", 0.25)])
            excerpt = (
                f"Clause '{clause_ref}' differs from the template wording but preserves the underlying obligation."
                if ai_tier == "low" else
                f"Clause '{clause_ref}' materially narrows the Vendor's obligation relative to the template."
            )

        effective_tier = ai_tier  # matches the deterministic floor: no rule match -> AI verdict stands
        if ai_tier == "prohibited":
            effective_tier = "prohibited"  # rule match always forces prohibited (14.1) -- already is here

        decided_at = clock.advance(hours=rng.uniform(0.5, 6))
        decider = pick(decided_by_users) if decided_by_users else None
        dev = Deviation(
            bank_guarantee_id=bg.id,
            clause_reference=clause_ref,
            template_text_summary=template_summary,
            bg_text_excerpt=excerpt,
            deviation_type=deviation_type,
            ai_proposed_tier=ai_tier,
            effective_tier=effective_tier,
            status="pending",
            is_missing_critical_clause=is_missing,
            created_at=clock.now(),
        )
        db.session.add(dev)
        db.session.flush()

        # Admin override for a small slice of prohibited-tier deviations so
        # the demo shows the override path actually clearing the block
        # (Section 14.1) -- most prohibited deviations are left un-overridden
        # (still hard-blocking) so the block itself is also visible.
        if effective_tier == "prohibited" and maybe(0.45):
            admin = decided_by_users[0] if decided_by_users else None
            override_at = decided_at + timedelta(hours=rng.uniform(1, 20))
            if override_at > NOW:
                override_at = NOW
            dev.admin_override_by = admin.id if admin else None
            dev.admin_override_at = override_at
            dev.admin_override_reason = (
                "Commercial team confirmed this is a boilerplate clause carried over from the vendor's "
                "master agreement and accepted the risk; documented and approved by the platform administrator."
            )
            db.session.add(AuditLog(
                event_type="prohibited_override_granted", actor_id=dev.admin_override_by,
                target_type="deviation", target_id=str(dev.id),
                metadata_json={"bg_number": bg.bg_number, "clause_reference": clause_ref,
                               "reason": dev.admin_override_reason},
                created_at=override_at,
            ))
            _bump("AuditLog")
        else:
            # Ordinary decision, recorded once (Section 13 simplification --
            # see SEED_DATA_PLAN.md for why this isn't re-decided per stage).
            dev.decided_by = decider.id if decider else None
            dev.decided_at = decided_at
            dev.decision_comment = (
                "Acceptable as-is; substance matches the template." if effective_tier == "low"
                else "Escalated for elevated review given the materiality of this change."
            ) if effective_tier != "prohibited" else None
            dev.status = "rejected" if effective_tier == "prohibited" else "accepted"
            if decider:
                db.session.add(AuditLog(
                    event_type="deviation_decision", actor_id=decider.id,
                    target_type="bank_guarantee", target_id=str(bg.id),
                    metadata_json={"deviation_id": dev.id, "clause_reference": clause_ref,
                                   "decision": dev.status, "comment": dev.decision_comment},
                    created_at=decided_at,
                ))
                _bump("AuditLog")

        rows.append(dev)
        _bump("Deviation")
    return rows


def _checklist_for(deviations):
    sections = [
        {"key": "header", "items": ["beneficiary_identity", "amount_and_currency", "guarantee_type"]},
        {"key": "body", "items": ["unconditional_and_irrevocable", "first_demand_payable", "validity_period"]},
        {"key": "closing", "items": ["return_on_expiry", "claim_window", "signature_authority"]},
    ]
    missing_refs = {d.clause_reference for d in deviations if d.is_missing_critical_clause}
    checklist = []
    for section in sections:
        for item in section["items"]:
            mandatory = item in ("beneficiary_identity", "amount_and_currency", "unconditional_and_irrevocable", "first_demand_payable")
            passed = item not in missing_refs
            checklist.append({"item": item, "section": section["key"], "mandatory": mandatory, "passed": passed})
    return checklist


def _risk_summary(deviations):
    if not deviations:
        return "No deviations identified."
    tiers = [d.effective_tier for d in deviations if d.effective_tier]
    if "prohibited" in tiers:
        summary = "Contains prohibited-tier clause deviation(s); special handling required at approval."
    elif "high" in tiers:
        summary = "Contains high-tier clause deviation(s); requires reviewer attention."
    elif tiers:
        summary = "Contains low-tier clause deviation(s); standard review applies."
    else:
        summary = "No deviations identified."
    missing = [d for d in deviations if d.is_missing_critical_clause]
    if missing:
        summary += f" {len(missing)} critical clause(s) missing."
    return summary


# --------------------------------------------------------------------------
# 5. Intake AI pipeline simulation  (Section 4 #5/#6/#9/#15/#16; Section 9.7)
# --------------------------------------------------------------------------

def _simulate_intake_pipeline(bg, creator, sap_code, clock, is_extension, po_records,
                               requires_ceo_cfo, decided_by_users, n_deviations):
    """Document + DocumentAnalysis + the 4-stage CeleryJob pipeline +
    AiInteractions + Deviations. Advances `clock`; leaves bg.current_stage
    == 'ready_for_review' and returns the created Deviation list."""
    doc_type = "extended_bg" if is_extension else "original_bg"
    upload_t = clock.advance(minutes=rng.randint(2, 25))
    doc = Document(
        bank_guarantee_id=bg.id, document_type=doc_type,
        storage_path=f"uploads/{bg.bg_number.lower()}_original.pdf",
        original_filename=f"{bg.bg_number}_bank_guarantee.pdf",
        mime_type="application/pdf",
        file_size_bytes=rng.randint(180_000, 2_400_000),
        uploaded_by=creator.id, uploaded_at=upload_t,
    )
    db.session.add(doc)
    db.session.flush()
    _bump("Document")

    event_type = "bg_extension_intake_started" if is_extension else "bg_intake_started"
    db.session.add(AuditLog(
        event_type=event_type, actor_id=creator.id, target_type="bank_guarantee",
        target_id=str(bg.id), metadata_json={"bg_number": bg.bg_number}, created_at=upload_t,
    ))
    _bump("AuditLog")

    # ---- Stage 1: bg_extraction ----
    t1_start = clock.advance(minutes=rng.randint(1, 3))
    t1_end = clock.advance(minutes=rng.randint(2, 6))
    db.session.add(CeleryJob(task_name="bg_extraction", status="completed", related_bg_id=bg.id,
                              triggered_by=creator.id, created_at=t1_start, completed_at=t1_end))
    _bump("CeleryJob")
    db.session.add(AiInteraction(
        feature="bg_extraction_and_checklist", related_bg_id=bg.id, user_id=creator.id,
        model_version="gemini-2.5-flash", prompt_token_count=rng.randint(2200, 4800),
        response_token_count=rng.randint(400, 900), latency_ms=rng.randint(1800, 5200),
        status="success", created_at=t1_end,
    ))
    _bump("AiInteraction")

    classification = {"is_bank_guarantee": True, "detected_bg_type": bg.bg_type, "type_matches": True}
    extracted_fields = {
        "bg_number": bg.bg_number, "amount": str(bg.amount), "currency": bg.currency,
        "issue_date": str(bg.issue_date), "expiry_date": str(bg.expiry_date),
        "claim_expiry_date": str(bg.claim_expiry_date) if bg.claim_expiry_date else None,
        "issuing_bank": bg.issuing_bank, "issuing_bank_branch": f"{(bg.issuing_bank or '').split()[0] if bg.issuing_bank else ''} Corporate Branch".strip(),
        "vendor_name": bg.vendor_name, "notes": None,
        "field_confidences": {
            "amount": {"confidence": round(rng.uniform(0.86, 0.99), 2), "note": None},
            "expiry_date": {"confidence": round(rng.uniform(0.82, 0.98), 2), "note": None},
            "issuing_bank": {"confidence": round(rng.uniform(0.9, 0.99), 2), "note": None},
        },
    }

    # ---- Stage 2a/2b (parallel): po_sap_cross_check + template_compliance ----
    t2_start = clock.advance(minutes=rng.randint(1, 2))
    t2a_end = clock.advance(minutes=rng.randint(2, 5))
    db.session.add(CeleryJob(task_name="po_sap_cross_check", status="completed", related_bg_id=bg.id,
                              triggered_by=creator.id, created_at=t2_start, completed_at=t2a_end))
    _bump("CeleryJob")

    checks = [
        {"check": "amount_tolerance", "status": "pass",
         "detail": f"BG amount {bg.amount} is within PO value {sum(float(p.po_value) for p in po_records):.2f}.",
         "explanation": None},
        {"check": "expiry_min_date", "status": "pass",
         "detail": f"Expiry {bg.expiry_date} is beyond the minimum validity date.", "explanation": None},
        {"check": "vendor_match", "status": "pass",
         "detail": f"Vendor '{bg.vendor_name}' matches the PO vendor.", "explanation": None},
    ]
    if maybe(0.32):
        idx = rng.randrange(len(checks))
        checks[idx]["status"] = "warning"
        checks[idx]["explanation"] = (
            f"This cross-check ({checks[idx]['check']}) needs your review: {checks[idx]['detail']} "
            "Please confirm the figures against the source document before proceeding."
        )
        db.session.add(AiInteraction(
            feature="cross_check_explanation", related_bg_id=bg.id, user_id=creator.id,
            model_version="gemini-2.5-flash", prompt_token_count=rng.randint(300, 700),
            response_token_count=rng.randint(80, 220), latency_ms=rng.randint(900, 2400),
            status="success", created_at=t2a_end,
        ))
        _bump("AiInteraction")
    if maybe(0.2):
        db.session.add(AiInteraction(
            feature="vendor_similarity", related_bg_id=bg.id, user_id=creator.id,
            model_version="gemini-2.5-flash", prompt_token_count=rng.randint(150, 350),
            response_token_count=rng.randint(60, 150), latency_ms=rng.randint(700, 1600),
            status="success", created_at=t2a_end,
        ))
        _bump("AiInteraction")

    po_sap_result = {
        "po_context": [{
            "po_number": p.po_number, "vendor_name": p.vendor_name, "po_value": str(p.po_value),
            "currency": p.currency,
            "open_advance_amount": str(p.open_advance_amount) if p.open_advance_amount is not None else None,
            "is_executed": bool(p.is_executed),
        } for p in po_records],
        "checks": checks, "shortfall": None,
    }
    if bg.bg_type == "abg":
        total_open = sum(float(p.open_advance_amount or 0) for p in po_records)
        po_sap_result["shortfall"] = {"blocked": False, "total_open": str(total_open)}

    t2b_end = clock.advance(minutes=rng.randint(3, 7))
    db.session.add(CeleryJob(task_name="template_compliance", status="completed", related_bg_id=bg.id,
                              triggered_by=creator.id, created_at=t2_start, completed_at=t2b_end))
    _bump("CeleryJob")

    deviations = _create_deviations(bg, sap_code, clock, decided_by_users, requires_ceo_cfo, n_deviations)
    n_chunks = max(1, (len(deviations) + 5) // 6)
    for _ in range(n_chunks):
        db.session.add(AiInteraction(
            feature="template_clause_comparison", related_bg_id=bg.id, user_id=creator.id,
            model_version="gemini-2.5-flash", prompt_token_count=rng.randint(1400, 3200),
            response_token_count=rng.randint(300, 700), latency_ms=rng.randint(1600, 4200),
            status="success", created_at=t2b_end,
        ))
        _bump("AiInteraction")

    checklist_result = _checklist_for(deviations)
    dispatch_readiness = "needs_followup" if any(
        i.get("mandatory") and not i.get("passed") for i in checklist_result
    ) else "ready"

    db.session.add(DocumentAnalysis(
        document_id=doc.id, classification_result=classification, extracted_fields=extracted_fields,
        po_sap_result=po_sap_result, checklist_result=checklist_result,
        dispatch_readiness=dispatch_readiness, ai_model_version="gemini-2.5-flash",
        processing_duration_ms=int(max((t2b_end - t1_start).total_seconds(), 1) * 1000),
        created_at=t2b_end,
    ))
    _bump("DocumentAnalysis")

    # ---- Stage 3: finalize_validation (chord callback) ----
    t3 = clock.advance(minutes=rng.randint(1, 3))
    db.session.add(CeleryJob(task_name="finalize_validation", status="completed", related_bg_id=bg.id,
                              triggered_by=creator.id, created_at=t2_start, completed_at=t3))
    _bump("CeleryJob")

    bg.risk_tier_summary = _risk_summary(deviations)
    bg.current_stage = "ready_for_review"
    db.session.flush()
    return deviations, doc


# --------------------------------------------------------------------------
# 6. DoA approval chain walker  (Section 9.1)
# --------------------------------------------------------------------------

ROLE_TO_STATUS = {
    "buyer": "pending_buyer_approval",
    "tc_head": "pending_category_lead_approval",
    "bu_fc": "pending_fc_approval",
    "bu_cfmc": "pending_bu_cfmc_approval",
    "abex": "pending_abex_verification",
}

DISPATCH_MODES = ["courier", "cmr"]
COURIER_NAMES = ["Blue Dart", "DTDC", "Delhivery", "Professional Couriers", "FedEx India"]


def _create_intake_dispatch(bg, creator, clock, context_type="intake"):
    mode = pick(DISPATCH_MODES)
    at = clock.advance(minutes=rng.randint(5, 45))
    kwargs = dict(bank_guarantee_id=bg.id, context_type=context_type, dispatch_mode=mode,
                  dispatched_by=creator.id, dispatched_at=at)
    if mode == "courier":
        kwargs.update(courier_name=pick(COURIER_NAMES),
                      tracking_number=f"{rng.randint(100000000, 999999999)}IN")
    else:
        kwargs.update(cmr_deliverer_name=f"{pick(['Suresh','Ramesh','Vinod','Anil','Deepak'])} Kumar",
                      cmr_deliverer_mobile=f"9{rng.randint(100000000, 999999999)}",
                      cmr_deliverer_email=f"vendor.rep{rng.randint(100,999)}@{bg.vendor_name.split()[0].lower()}.example.com" if bg.vendor_name else None)
    db.session.add(Dispatch(**kwargs))
    _bump("Dispatch")
    return at


def _submit_bg(bg, creator, sap_code, users_by_role_sap, clock, is_extension):
    _create_intake_dispatch(bg, creator, clock,
                             context_type="extension" if is_extension else "intake")
    at = clock.advance(minutes=rng.randint(10, 90))
    bg.status = "pending_buyer_approval"
    bg.current_stage = "pending_buyer_approval"
    bg.updated_at = at
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage=None, to_stage="pending_buyer_approval",
        action=WorkflowAction.submit.value, actor_id=creator.id, actor_role=creator.active_role,
        created_at=at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="bg_submitted", actor_id=creator.id, target_type="bank_guarantee",
        target_id=str(bg.id),
        metadata_json={"bg_number": bg.bg_number, "is_extension": is_extension, "dispatch_mode": "courier"},
        created_at=at,
    ))
    _bump("AuditLog")
    for buyer in _role_holders(users_by_role_sap, "buyer", sap_code):
        _notify(buyer, "approval_queue_item", "New Bank Guarantee awaiting review",
                f"{bg.bg_number} ({bg.vendor_name or 'vendor'}, {bg.amount} {bg.currency}) "
                "has been submitted and awaits your approval.",
                f"/bg-multi-stage-approval/{bg.id}", at, triggered_by_id=creator.id)
    return at


def _advance(bg, clock, from_stage, to_stage, action, actor, actor_role, comment, notify_recipients,
             notify_type, notify_title, notify_body, is_verify=False):
    """One `approve_forward` / `verify` transition: WorkflowHistory + AuditLog
    (`bg_stage_advanced`) + CeleryJob(`workflow.notify_stage_transition`) +
    notifications to the recipients of the next stage."""
    at = clock.advance(hours=rng.uniform(2, 30))
    bg.status = to_stage
    bg.current_stage = to_stage
    bg.updated_at = at
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage=from_stage, to_stage=to_stage, action=action,
        actor_id=actor.id if actor else None, actor_role=actor_role, comments=comment, created_at=at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="bg_stage_advanced", actor_id=actor.id if actor else None,
        target_type="bank_guarantee", target_id=str(bg.id),
        metadata_json={"bg_number": bg.bg_number, "role": actor_role, "from": from_stage,
                       "to": to_stage, "verify": is_verify},
        created_at=at,
    ))
    _bump("AuditLog")
    db.session.add(CeleryJob(task_name="workflow.notify_stage_transition", status="completed",
                              related_bg_id=bg.id, triggered_by=actor.id if actor else None,
                              created_at=at, completed_at=at + timedelta(seconds=rng.randint(1, 15))))
    _bump("CeleryJob")
    for r in notify_recipients:
        _notify(r, notify_type, notify_title, notify_body, f"/bg/{bg.id}", at,
                triggered_by_id=actor.id if actor else None)
    return at


def _reject(bg, clock, from_stage, actor, actor_role, comment):
    at = clock.advance(hours=rng.uniform(2, 40))
    bg.status = "rejected"
    bg.current_stage = "rejected"
    bg.updated_at = at
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage=from_stage, to_stage="rejected",
        action=WorkflowAction.reject.value, actor_id=actor.id, actor_role=actor_role,
        comments=comment, created_at=at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="bg_rejected", actor_id=actor.id, target_type="bank_guarantee",
        target_id=str(bg.id), metadata_json={"bg_number": bg.bg_number, "role": actor_role},
        created_at=at,
    ))
    _bump("AuditLog")
    db.session.add(CeleryJob(task_name="workflow.notify_stage_transition", status="completed",
                              related_bg_id=bg.id, triggered_by=actor.id,
                              created_at=at, completed_at=at + timedelta(seconds=rng.randint(1, 15))))
    _bump("CeleryJob")
    creator = db.session.get(User, bg.creator_id)
    _notify(creator, "bg_rejected", "Your Bank Guarantee was rejected",
            f"{bg.bg_number} was rejected during review. You can view the reason on its record.",
            f"/bg/{bg.id}", at, triggered_by_id=actor.id)
    return at


REJECT_COMMENTS = {
    "buyer": "Vendor details do not match our records for this purchase order; please re-verify and resubmit.",
    "tc_head": "The clause deviations on this guarantee are not acceptable in their current form; please have the vendor's bank reissue the guarantee.",
    "bu_fc": "The guaranteed amount does not reconcile with the purchase order value on record; returning for correction.",
    "bu_cfmc": "The guaranteed amount does not reconcile with the purchase order value on record; returning for correction.",
}


def _run_doa_chain(bg, creator, sap_code, users_by_role_sap, clock, requires_ceo_cfo,
                    expenditure_type, target, reject_at=None):
    """Walk the BG through submit -> buyer -> tc_head -> finance ->
    [ceo_cfo] -> abex, stopping at `target` (one of "pending_buyer",
    "pending_category_lead", "pending_finance", "pending_ceo_cfo",
    "pending_abex", "live") or rejecting at `reject_at`
    (one of "buyer", "tc_head", "finance")."""
    finance_role = "bu_fc" if expenditure_type == "opex" else "bu_cfmc"
    coordinator = db.session.get(User, bg.coordinator_id) if bg.coordinator_id else None

    _submit_bg(bg, creator, sap_code, users_by_role_sap, clock, is_extension=bool(bg.parent_bg_id))
    if target == "pending_buyer":
        return

    buyer = _first_holder(users_by_role_sap, "buyer", sap_code) or creator
    if reject_at == "buyer":
        _reject(bg, clock, "pending_buyer_approval", buyer, "buyer", REJECT_COMMENTS["buyer"])
        return
    tc_head_holders = _role_holders(users_by_role_sap, "tc_head", sap_code)
    _advance(bg, clock, "pending_buyer_approval", "pending_category_lead_approval",
             WorkflowAction.approve_forward.value, buyer, "buyer",
             "Vendor and PO details confirmed; forwarding for category-lead review.",
             tc_head_holders, "approval_queue_item", f"{bg.bg_number} awaits Category Lead (TC Head) review",
             f"{bg.bg_number} has been forwarded and awaits your review.")
    if target == "pending_category_lead":
        return

    tc_head = tc_head_holders[0] if tc_head_holders else buyer
    if reject_at == "tc_head":
        _reject(bg, clock, "pending_category_lead_approval", tc_head, "tc_head", REJECT_COMMENTS["tc_head"])
        return
    finance_holders = _role_holders(users_by_role_sap, finance_role, sap_code)
    _advance(bg, clock, "pending_category_lead_approval", ROLE_TO_STATUS[finance_role],
             WorkflowAction.approve_forward.value, tc_head, "tc_head",
             "Clause deviations reviewed and tiered; forwarding to Finance.",
             finance_holders, "approval_queue_item",
             f"{bg.bg_number} awaits {'Finance (BU FC)' if finance_role == 'bu_fc' else 'Finance (BU CFMC)'} review",
             f"{bg.bg_number} has been forwarded and awaits your review.")
    if target == "pending_finance":
        return

    finance_user = finance_holders[0] if finance_holders else tc_head
    if reject_at == "finance":
        _reject(bg, clock, ROLE_TO_STATUS[finance_role], finance_user, finance_role, REJECT_COMMENTS[finance_role])
        return

    if requires_ceo_cfo:
        _advance(bg, clock, ROLE_TO_STATUS[finance_role], "pending_ceo_cfo",
                 WorkflowAction.approve_forward.value, finance_user, finance_role,
                 "Elevated risk tier detected; routing for CEO/CFO sign-off before ABEX verification.",
                 [creator], "ceo_cfo_required", "CEO/CFO sign-off required for your Bank Guarantee",
                 f"{bg.bg_number} requires elevated CEO/CFO sign-off. Please obtain it via email and "
                 "attach the evidence in the CEO/CFO Mail page.")
        if target == "pending_ceo_cfo":
            return
        abex_holders = _role_holders(users_by_role_sap, "abex", sap_code)
        at = clock.advance(hours=rng.uniform(6, 48))
        bg.status = "pending_abex_verification"
        bg.current_stage = "pending_abex_verification"
        bg.updated_at = at
        db.session.add(WorkflowHistory(
            bank_guarantee_id=bg.id, from_stage="pending_ceo_cfo", to_stage="pending_abex_verification",
            action=WorkflowAction.approve_forward.value, actor_id=creator.id, actor_role="creator",
            comments="CEO/CFO email evidence attached", created_at=at,
        ))
        _bump("WorkflowHistory")
        db.session.add(AuditLog(
            event_type="ceo_cfo_evidence_attached", actor_id=creator.id, target_type="bank_guarantee",
            target_id=str(bg.id), metadata_json={"bg_number": bg.bg_number, "reference": f"EML-{rng.randint(10000,99999)}"},
            created_at=at,
        ))
        _bump("AuditLog")
        db.session.add(Document(
            bank_guarantee_id=bg.id, document_type="offline_approval",
            storage_path=f"uploads/{bg.bg_number.lower()}_ceo_cfo_evidence.pdf",
            original_filename=f"{bg.bg_number}_ceo_cfo_email_evidence.pdf", mime_type="application/pdf",
            file_size_bytes=rng.randint(80_000, 400_000), uploaded_by=creator.id, uploaded_at=at,
        ))
        _bump("Document")
        db.session.add(CeleryJob(task_name="workflow.notify_stage_transition", status="completed",
                                  related_bg_id=bg.id, triggered_by=creator.id, created_at=at,
                                  completed_at=at + timedelta(seconds=rng.randint(1, 10))))
        _bump("CeleryJob")
        for holder in abex_holders:
            _notify(holder, "approval_queue_item", f"{bg.bg_number} awaits ABEX Verification review",
                    f"{bg.bg_number} has been forwarded and awaits your review.", f"/bg/{bg.id}", at,
                    triggered_by_id=creator.id)
    else:
        abex_holders = _role_holders(users_by_role_sap, "abex", sap_code)
        _advance(bg, clock, ROLE_TO_STATUS[finance_role], "pending_abex_verification",
                 WorkflowAction.approve_forward.value, finance_user, finance_role,
                 "Financials confirmed; forwarding for ABEX verification.",
                 abex_holders, "approval_queue_item", f"{bg.bg_number} awaits ABEX Verification review",
                 f"{bg.bg_number} has been forwarded and awaits your review.")
    if target == "pending_abex":
        return

    abex_user = _first_holder(users_by_role_sap, "abex", sap_code)
    if abex_user is None:
        abex_user = finance_user
    notify_list = [creator]
    if coordinator and coordinator.id != creator.id:
        notify_list.append(coordinator)
    _advance(bg, clock, "pending_abex_verification", "live", WorkflowAction.verify.value,
             abex_user, "abex", "Verified against the executed template and financial records; activating.",
             notify_list, "bg_live", "Your Bank Guarantee is now Live",
             f"{bg.bg_number} has been verified and activated. It is now Live.", is_verify=True)


# --------------------------------------------------------------------------
# 7. Per-BG construction helpers
# --------------------------------------------------------------------------

_BG_COUNTER = [0]


def _next_bg_number():
    _BG_COUNTER[0] += 1
    return f"BG-{NOW.year}-{_BG_COUNTER[0]:06d}"


def _pick_vendor_and_pos(vendor_map, max_pos=2):
    vendor = pick(list(vendor_map.keys()))
    pos = vendor_map[vendor]
    n = min(len(pos), rng.randint(1, max_pos))
    chosen = rng.sample(pos, n)
    return vendor, chosen


def _pick_amount(bg_type, po_records):
    total_po = sum(float(p.po_value) for p in po_records)
    if bg_type == "abg":
        total_open = sum(float(p.open_advance_amount or 0) for p in po_records)
        floor_amt = max(total_open * rng.uniform(1.1, 1.6), total_open + 50_000)
        amount = max(floor_amt, total_po * rng.uniform(0.15, 0.35))
    else:
        amount = total_po * rng.uniform(0.06, 0.3)
    return Decimal(str(round(amount, 2)))


def _pick_currency():
    return weighted_pick([("INR", 0.955), ("USD", 0.02), ("EUR", 0.015), ("GBP", 0.01)])


def _validity_months(bg_type):
    return {"pbg": (12, 24), "abg": (9, 18), "cpbg": (18, 36), "cpbg_cum_pbg": (18, 36), "cg": (24, 60)}.get(bg_type, (12, 24))


def _gen_dates(bg_type, created_at, days_to_expiry=None):
    """issue_date shortly before intake; expiry per BG-type validity window
    unless `days_to_expiry` pins it (used for extension/invocation zones)."""
    issue_date = (created_at - timedelta(days=rng.randint(0, 10))).date()
    if days_to_expiry is not None:
        expiry_date = (NOW + timedelta(days=days_to_expiry)).date()
    else:
        lo, hi = _validity_months(bg_type)
        expiry_date = issue_date + timedelta(days=rng.randint(lo * 30, hi * 30))
    claim_expiry = None
    if maybe(0.8):
        claim_expiry = expiry_date + timedelta(days=rng.randint(30, 180))
    return issue_date, expiry_date, claim_expiry


def _new_bg_shell(creator, coordinator, sap, bg_type, format_variant, expenditure_type,
                   vendor, po_records, amount, currency, issuing_bank,
                   issue_date, expiry_date, claim_expiry_date, created_at, parent_bg_id=None):
    bg = BankGuarantee(
        bg_number=_next_bg_number(), parent_bg_id=parent_bg_id, bg_type=bg_type,
        format_variant=format_variant, expenditure_type=expenditure_type, sap_system_id=sap.id,
        amount=amount, currency=currency, issue_date=issue_date, expiry_date=expiry_date,
        claim_expiry_date=claim_expiry_date, issuing_bank=issuing_bank, vendor_name=vendor,
        po_numbers=[p.po_number for p in po_records], status="draft", current_stage="validating",
        saved_as_draft=False, creator_id=creator.id,
        coordinator_id=(coordinator.id if coordinator else None),
        created_at=created_at, updated_at=created_at,
    )
    db.session.add(bg)
    db.session.flush()
    _bump("BankGuarantee")
    return bg


def _depth_created_at(depth):
    """Section 6 temporal distribution, applied per lifecycle depth so a
    BG's created_at leaves enough runway for everything it needs to have
    gone through by "now"."""
    if depth == "shallow":       # draft_only / in_flight -- currently early
        return days_ago(weighted_pick([(rng.randint(0, 1), 0.35), (rng.randint(1, 7), 0.4), (rng.randint(7, 20), 0.25)]),
                         hour=rng.randint(7, 21))
    if depth == "rejected":       # a completed-but-early lifecycle event
        return days_ago(rng.randint(3, 55), hour=rng.randint(7, 20))
    if depth == "medium":         # plain_live / invocation & extension monitoring-only
        return days_ago(rng.randint(12, 80), hour=rng.randint(6, 22))
    # "deep": extension-uploaded / invocation-sent / closure / return -- needs
    # the most runway to plausibly have finished a full multi-week journey.
    return days_ago(rng.randint(35, 90), hour=rng.randint(6, 22))


# --------------------------------------------------------------------------
# 8. Bank verification  (Section 4 #22; Section 9.6; 14.5)
# --------------------------------------------------------------------------

BANK_CONTACT_EMAILS = {
    "State Bank of India": "bgclaims@sbi.co.in",
    "HDFC Bank": "claims@hdfcbank.com",
    "ICICI Bank": "claims@icicibank.com",
    "Punjab National Bank": "",   # seeded with a blank contact -> genuine "no_contact" edge case
}


def _do_bank_verification(bg, clock, users_by_role_sap, sap_code):
    at = clock.advance(minutes=rng.randint(10, 90))
    contact = BANK_CONTACT_EMAILS.get(bg.issuing_bank)
    verification = BankVerification(bank_guarantee_id=bg.id, status="pending",
                                     bank_contact_email=contact or None, created_at=at)
    db.session.add(verification)
    db.session.flush()
    _bump("BankVerification")

    if not contact:
        verification.status = "not_sent"
        db.session.add(AuditLog(event_type="bank_verification_no_contact", target_type="bank_verification",
                                 target_id=str(verification.id), metadata_json={"bg_number": bg.bg_number},
                                 created_at=at))
        _bump("AuditLog")
        return verification

    verification.sent_at = at
    verification.verification_token = _fake_token()   # live until the bank clicks or it's overridden
    db.session.add(AuditLog(event_type="bank_verification_sent", target_type="bank_verification",
                             target_id=str(verification.id),
                             metadata_json={"bg_number": bg.bg_number, "email": contact}, created_at=at))
    _bump("AuditLog")

    age_days = (NOW - at).total_seconds() / 86400.0
    if age_days < 1.5:
        return verification  # very recently Live -- bank hasn't had time to respond yet

    outcome = weighted_pick([("confirmed", 0.76), ("disputed", 0.04), ("no_response", 0.12), ("pending", 0.08)])
    if outcome == "confirmed":
        conf_at = min(at + timedelta(hours=rng.uniform(2, 44)), NOW)
        verification.confirmed_at = conf_at
        verification.status = "confirmed"
        verification.verification_token = None   # cleared the instant the bank's link is used (14.2)
        db.session.add(AuditLog(event_type="bank_verification_confirmed", target_type="bank_verification",
                                 target_id=str(verification.id), metadata_json={"bg_number": bg.bg_number},
                                 created_at=conf_at))
        _bump("AuditLog")
    elif outcome == "disputed":
        disp_at = min(at + timedelta(hours=rng.uniform(3, 44)), NOW)
        verification.status = "disputed"
        verification.verification_token = None
        db.session.add(AuditLog(event_type="bank_verification_disputed", target_type="bank_verification",
                                 target_id=str(verification.id), metadata_json={"bg_number": bg.bg_number},
                                 created_at=disp_at))
        _bump("AuditLog")
    elif outcome == "no_response":
        poll_at = min(at + timedelta(hours=rng.uniform(49, 96)), NOW)
        verification.status = "no_response"
        verification.last_polled_at = poll_at
        for c in _role_holders(users_by_role_sap, "coordinator", sap_code):
            _notify(c, "bank_verification_no_response", "Bank verification awaiting response",
                    f"The bank has not responded to the verification request for {bg.bg_number}.",
                    f"/bg/{bg.id}", poll_at, triggered_by_id=None)
        if maybe(0.35):
            override_at = min(poll_at + timedelta(hours=rng.uniform(4, 40)), NOW)
            coordinator = pick(_role_holders(users_by_role_sap, "coordinator", sap_code)) if _role_holders(users_by_role_sap, "coordinator", sap_code) else None
            verification.status = "confirmed"
            verification.confirmed_at = override_at
            verification.verification_token = None
            verification.response_reference = f"Manually confirmed via phone call, bank reference BGV-{rng.randint(100000, 999999)}."
            db.session.add(AuditLog(event_type="bank_verification_manual_override",
                                     actor_id=coordinator.id if coordinator else None,
                                     target_type="bank_verification", target_id=str(verification.id),
                                     metadata_json={"bg_number": bg.bg_number, "status": "confirmed",
                                                    "reference": verification.response_reference},
                                     created_at=override_at))
            _bump("AuditLog")
    return verification


# --------------------------------------------------------------------------
# 9. Closure + segregation-of-duties verify helper  (Section 4 #12; 9.3; 14.4)
# --------------------------------------------------------------------------

def _verify_closure(bg, closure, clock, users_by_role_sap, sap_code, exclude=None):
    exclude = set(exclude or [])
    exclude.add(closure.initiated_by)
    candidates = [u for u in _role_holders(users_by_role_sap, "abex", sap_code) if u.id not in exclude]
    abex_user = candidates[0] if candidates else _first_holder(users_by_role_sap, "abex", sap_code)
    at = clock.advance(hours=rng.uniform(4, 60))
    closure.verified_by = abex_user.id if abex_user else None
    closure.closed_at = at
    closure.stage = "closed"
    bg.status = "closed"
    bg.current_stage = "closed"
    bg.updated_at = at
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="pending_abex_verification", to_stage="closed",
        action=WorkflowAction.closure_verified.value, actor_id=closure.verified_by, actor_role="abex",
        comments=closure.eligibility_reasoning, created_at=at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="closure_verified", actor_id=closure.verified_by, target_type="bg_closure",
        target_id=str(closure.id), metadata_json={"bg_number": bg.bg_number, "closed_at": str(at)},
        created_at=at,
    ))
    _bump("AuditLog")


def _do_closure(bg, clock, users_by_role_sap, sap_code, po_records, outcome):
    """outcome in: standard_closed, exception_closed, exception_offline_closed,
    exception_in_progress_cfo, exception_in_progress_ceo, exception_declined_ceo,
    tc_rejected."""
    po_executed = all(p.is_executed for p in po_records)
    total_open = sum(float(p.open_advance_amount or 0) for p in po_records) if bg.bg_type == "abg" else 0.0
    standard = bool(po_executed) and (bg.bg_type != "abg" or total_open == 0)
    is_exception = not standard
    po_list = ", ".join(p.po_number for p in po_records)

    lines = [f"Underlying PO/contract ({po_list}) fully executed: {'Yes' if po_executed else 'No'}."]
    if bg.bg_type == "abg":
        lines.append(f"Open advance amount: {int(total_open)}.")
        lines.append("Advance fully recovered" if total_open == 0 else "Advance still open.")
    reasoning = " ".join(lines)
    reasoning += (
        " Closure is STANDARD (proceeds directly to ABEX verification)." if standard
        else " Closure is an EXCEPTION and requires category-lead review and executive sign-off."
    )

    coordinator = db.session.get(User, bg.coordinator_id)
    if coordinator is None:
        coordinator = pick(_role_holders(users_by_role_sap, "coordinator", sap_code))
    initiated_at = clock.advance(days=rng.uniform(0.5, 4))
    stage0 = "pending_category_lead" if is_exception else "pending_abex_verification"

    closure = BgClosure(
        bank_guarantee_id=bg.id, is_exception=is_exception, eligibility_reasoning=reasoning,
        exception_justification=(
            "The underlying contract has been executed in substance; the vendor's final invoice "
            "reconciliation is pending but poses no financial risk to closing this guarantee."
            if is_exception else None
        ),
        stage=stage0, initiated_by=coordinator.id if coordinator else None, created_at=initiated_at,
    )
    db.session.add(closure)
    db.session.flush()
    _bump("BgClosure")
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="live", to_stage=stage0,
        action=WorkflowAction.closure_initiated.value, actor_id=coordinator.id if coordinator else None,
        actor_role="coordinator", comments=reasoning, created_at=initiated_at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="closure_initiated", actor_id=coordinator.id if coordinator else None,
        target_type="bg_closure", target_id=str(closure.id),
        metadata_json={"bg_number": bg.bg_number, "is_exception": is_exception, "stage": stage0},
        created_at=initiated_at,
    ))
    _bump("AuditLog")

    if not is_exception:
        if outcome == "standard_closed":
            _verify_closure(bg, closure, clock, users_by_role_sap, sap_code)
        return closure

    tc_head = pick(_role_holders(users_by_role_sap, "tc_head", sap_code))
    review_at = min(closure.created_at + timedelta(hours=rng.uniform(4, 60)), NOW)
    clock.t = max(clock.t, review_at)

    if outcome == "tc_rejected":
        closure.stage = "closed"
        db.session.add(WorkflowHistory(
            bank_guarantee_id=bg.id, from_stage="pending_category_lead", to_stage="closed",
            action=WorkflowAction.closure_rejected.value, actor_id=tc_head.id if tc_head else None,
            actor_role="tc_head",
            comments="Exception justification insufficient; the PO must be fully closed before this "
                     "guarantee can be released.", created_at=review_at,
        ))
        _bump("WorkflowHistory")
        db.session.add(AuditLog(
            event_type="closure_reviewed", actor_id=tc_head.id if tc_head else None,
            target_type="bg_closure", target_id=str(closure.id),
            metadata_json={"bg_number": bg.bg_number, "decision": "reject"}, created_at=review_at,
        ))
        _bump("AuditLog")
        return closure

    closure.stage = "pending_cfo"
    closure.cfo_approval_token = _fake_token()   # live until CFO clicks (14.2)
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="pending_category_lead", to_stage="pending_cfo",
        action=WorkflowAction.closure_reviewed.value, actor_id=tc_head.id if tc_head else None,
        actor_role="tc_head", comments="Justification accepted; routing for CFO sign-off.",
        created_at=review_at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="closure_reviewed", actor_id=tc_head.id if tc_head else None, target_type="bg_closure",
        target_id=str(closure.id), metadata_json={"bg_number": bg.bg_number, "decision": "approve"},
        created_at=review_at,
    ))
    _bump("AuditLog")

    if outcome == "exception_in_progress_cfo":
        return closure

    if outcome == "exception_offline_closed":
        offline_at = min(review_at + timedelta(hours=rng.uniform(4, 48)), NOW)
        clock.t = max(clock.t, offline_at)
        closure.stage = "pending_abex_verification"
        db.session.add(Document(
            bank_guarantee_id=bg.id, document_type="offline_approval",
            storage_path=f"uploads/{bg.bg_number.lower()}_closure_offline_evidence.pdf",
            original_filename=f"{bg.bg_number}_closure_cfo_ceo_evidence.pdf", mime_type="application/pdf",
            file_size_bytes=rng.randint(90_000, 300_000), uploaded_by=coordinator.id if coordinator else None,
            uploaded_at=offline_at,
        ))
        _bump("Document")
        db.session.add(WorkflowHistory(
            bank_guarantee_id=bg.id, from_stage="pending_approval_attachments",
            to_stage="pending_abex_verification", action=WorkflowAction.closure_reviewed.value,
            actor_id=coordinator.id if coordinator else None, actor_role="coordinator",
            comments="Combined offline CFO/CEO sign-off evidence attached.", created_at=offline_at,
        ))
        _bump("WorkflowHistory")
        db.session.add(AuditLog(
            event_type="closure_offline_attachments", actor_id=coordinator.id if coordinator else None,
            target_type="bg_closure", target_id=str(closure.id),
            metadata_json={"bg_number": bg.bg_number}, created_at=offline_at,
        ))
        _bump("AuditLog")
        _verify_closure(bg, closure, clock, users_by_role_sap, sap_code, exclude={tc_head.id if tc_head else -1})
        return closure

    cfo_at = min(review_at + timedelta(hours=rng.uniform(6, 60)), NOW)
    clock.t = max(clock.t, cfo_at)
    closure.stage = "pending_ceo"
    closure.cfo_approved_at = cfo_at
    closure.cfo_approval_token = None   # cleared the instant it's consumed (14.2)
    closure.ceo_approval_token = _fake_token()   # live until CEO clicks
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="pending_cfo", to_stage="pending_ceo",
        action=WorkflowAction.executive_approved.value, actor_role="cfo",
        comments="CFO approved closure via magic link.", created_at=cfo_at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="executive_approved", target_type="bg_closure", target_id=str(closure.id),
        metadata_json={"bg_number": bg.bg_number, "role": "cfo", "closure_id": closure.id},
        created_at=cfo_at,
    ))
    _bump("AuditLog")

    if outcome == "exception_in_progress_ceo":
        return closure

    if outcome == "exception_declined_ceo":
        decline_at = min(cfo_at + timedelta(hours=rng.uniform(4, 48)), NOW)
        clock.t = max(clock.t, decline_at)
        closure.stage = "closed"
        closure.ceo_approval_token = None
        db.session.add(WorkflowHistory(
            bank_guarantee_id=bg.id, from_stage="pending_ceo", to_stage="closed",
            action=WorkflowAction.executive_declined.value, actor_role="ceo",
            comments="CEO declined closure via magic link.", created_at=decline_at,
        ))
        _bump("WorkflowHistory")
        db.session.add(AuditLog(
            event_type="executive_declined", target_type="bg_closure", target_id=str(closure.id),
            metadata_json={"bg_number": bg.bg_number, "role": "ceo"}, created_at=decline_at,
        ))
        _bump("AuditLog")
        return closure

    ceo_at = min(cfo_at + timedelta(hours=rng.uniform(6, 60)), NOW)
    clock.t = max(clock.t, ceo_at)
    closure.stage = "pending_abex_verification"
    closure.ceo_approved_at = ceo_at
    closure.ceo_approval_token = None
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="pending_ceo", to_stage="pending_abex_verification",
        action=WorkflowAction.executive_approved.value, actor_role="ceo",
        comments="CEO approved closure via magic link.", created_at=ceo_at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="executive_approved", target_type="bg_closure", target_id=str(closure.id),
        metadata_json={"bg_number": bg.bg_number, "role": "ceo", "closure_id": closure.id},
        created_at=ceo_at,
    ))
    _bump("AuditLog")

    if outcome == "exception_closed":
        _verify_closure(bg, closure, clock, users_by_role_sap, sap_code, exclude={tc_head.id if tc_head else -1})
    return closure


# --------------------------------------------------------------------------
# 10. Physical return  (Section 4 #10, #13; Section 9.4)
# --------------------------------------------------------------------------

def _do_return(bg, clock, users_by_role_sap, sap_code, stage):
    """stage in {"requested", "dispatched", "receipt_confirmed"}."""
    requester = db.session.get(User, bg.coordinator_id)
    if requester is None:
        requester = pick(_role_holders(users_by_role_sap, "coordinator", sap_code))
    at = clock.advance(days=rng.uniform(0.5, 5))
    ret = BgReturn(bank_guarantee_id=bg.id, status="requested",
                    requested_by=requester.id if requester else None)
    db.session.add(ret)
    db.session.flush()
    _bump("BgReturn")
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage=bg.status, to_stage="return_requested",
        action=WorkflowAction.return_requested.value, actor_id=requester.id if requester else None,
        actor_role="coordinator", created_at=at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="return_requested", actor_id=requester.id if requester else None,
        target_type="bg_return", target_id=str(ret.id), metadata_json={"bg_number": bg.bg_number},
        created_at=at,
    ))
    _bump("AuditLog")
    if stage == "requested":
        return ret

    dispatch_at = clock.advance(days=rng.uniform(0.5, 6))
    mode = pick(DISPATCH_MODES)
    kwargs = dict(bank_guarantee_id=bg.id, context_type="return", dispatch_mode=mode,
                  dispatched_by=requester.id if requester else None, dispatched_at=dispatch_at)
    if mode == "courier":
        kwargs.update(courier_name=pick(COURIER_NAMES), tracking_number=f"{rng.randint(100000000, 999999999)}IN")
    else:
        kwargs.update(cmr_deliverer_name=f"{pick(['Suresh', 'Ramesh', 'Vinod', 'Anil'])} Kumar",
                       cmr_deliverer_mobile=f"9{rng.randint(100000000, 999999999)}")
    dispatch_row = Dispatch(**kwargs)
    db.session.add(dispatch_row)
    db.session.flush()
    _bump("Dispatch")
    ret.dispatch_id = dispatch_row.id
    ret.status = "dispatched"
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="return_requested", to_stage="return_dispatched",
        action=WorkflowAction.return_dispatched.value, actor_id=requester.id if requester else None,
        actor_role="coordinator", created_at=dispatch_at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="return_dispatched", actor_id=requester.id if requester else None,
        target_type="bg_return", target_id=str(ret.id),
        metadata_json={"bg_number": bg.bg_number, "mode": mode}, created_at=dispatch_at,
    ))
    _bump("AuditLog")
    if stage == "dispatched":
        return ret

    confirm_at = clock.advance(days=rng.uniform(1, 8))
    ret.status = "receipt_confirmed"
    ret.receipt_confirmed_by = requester.id if requester else None
    ret.receipt_confirmed_at = confirm_at
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="return_dispatched", to_stage="return_receipt_confirmed",
        action=WorkflowAction.return_receipt_confirmed.value, actor_id=requester.id if requester else None,
        actor_role="coordinator", created_at=confirm_at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="return_receipt_confirmed", actor_id=requester.id if requester else None,
        target_type="bg_return", target_id=str(ret.id), metadata_json={"bg_number": bg.bg_number},
        created_at=confirm_at,
    ))
    _bump("AuditLog")
    return ret


# --------------------------------------------------------------------------
# 11. Invocation  (Section 4 #8, #14; Section 9.5; 14.7, 14.8)
# --------------------------------------------------------------------------

def _do_invocation(bg, clock, users_by_role_sap, sap_code, stage):
    """stage in {"approaching_window", "critical", "draft_generated",
    "signed_uploaded", "on_hold", "sent_to_bank"}."""
    inv = BgInvocation(bank_guarantee_id=bg.id, stage="approaching_window", created_at=clock.now())
    db.session.add(inv)
    db.session.flush()
    _bump("BgInvocation")
    if stage in ("approaching_window", "critical"):
        inv.stage = stage
        return inv

    bu_fc_user = pick(_role_holders(users_by_role_sap, "bu_fc", sap_code)) \
        or pick(_role_holders(users_by_role_sap, "buyer", sap_code))
    draft_at = clock.advance(hours=rng.uniform(2, 30))
    db.session.add(CeleryJob(task_name="invocation.generate_draft", status="completed", related_bg_id=bg.id,
                              triggered_by=bu_fc_user.id if bu_fc_user else None, created_at=draft_at,
                              completed_at=draft_at + timedelta(minutes=rng.randint(1, 4))))
    _bump("CeleryJob")
    ai = AiInteraction(feature="invocation_letter_content", related_bg_id=bg.id,
                        user_id=bu_fc_user.id if bu_fc_user else None, model_version="gemini-2.5-flash",
                        prompt_token_count=rng.randint(500, 1100), response_token_count=rng.randint(200, 450),
                        latency_ms=rng.randint(1400, 3600), status="success", created_at=draft_at)
    db.session.add(ai)
    db.session.flush()
    _bump("AiInteraction")

    version = 1
    gen_docx = GeneratedDocument(bank_guarantee_id=bg.id, document_kind="invocation_letter",
                                  storage_path=f"generated/invocation_{bg.id}_v{version}.docx", file_format="docx",
                                  generated_by_user_id=bu_fc_user.id if bu_fc_user else None,
                                  source_ai_interaction_id=ai.id, version=version, generated_at=draft_at)
    gen_pdf = GeneratedDocument(bank_guarantee_id=bg.id, document_kind="invocation_letter",
                                 storage_path=f"generated/invocation_{bg.id}_v{version}.pdf", file_format="pdf",
                                 generated_by_user_id=bu_fc_user.id if bu_fc_user else None,
                                 source_ai_interaction_id=ai.id, version=version, generated_at=draft_at)
    db.session.add_all([gen_docx, gen_pdf])
    db.session.flush()
    _bump("GeneratedDocument", 2)
    inv.stage = "draft_generated"
    inv.draft_document_id = gen_docx.id   # points at the docx row specifically (14.7)
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="approaching_window", to_stage="draft_generated",
        action=WorkflowAction.invocation_draft_generated.value, actor_id=bu_fc_user.id if bu_fc_user else None,
        actor_role="bu_fc", comments=f"Invocation draft generated (version {version}).", created_at=draft_at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="invocation_draft_generated", actor_id=bu_fc_user.id if bu_fc_user else None,
        target_type="bg_invocation", target_id=str(inv.id),
        metadata_json={"bg_number": bg.bg_number, "version": version, "docx_id": gen_docx.id, "pdf_id": gen_pdf.id},
        created_at=draft_at,
    ))
    _bump("AuditLog")
    if stage == "draft_generated":
        return inv

    sign_at = clock.advance(hours=rng.uniform(4, 48))
    doc = Document(bank_guarantee_id=bg.id, document_type="signed_invocation_letter",
                    storage_path=f"uploads/{bg.bg_number.lower()}_signed_invocation.pdf",
                    original_filename=f"{bg.bg_number}_signed_invocation_letter.pdf", mime_type="application/pdf",
                    file_size_bytes=rng.randint(60_000, 220_000), uploaded_by=bu_fc_user.id if bu_fc_user else None,
                    uploaded_at=sign_at)
    db.session.add(doc)
    db.session.flush()
    _bump("Document")
    inv.signed_document_id = doc.id
    inv.stage = "signed_uploaded"
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="draft_generated", to_stage="signed_uploaded",
        action=WorkflowAction.invocation_signed_uploaded.value, actor_id=bu_fc_user.id if bu_fc_user else None,
        actor_role="bu_fc", comments="Signed invocation letter uploaded.", created_at=sign_at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="invocation_signed_uploaded", actor_id=bu_fc_user.id if bu_fc_user else None,
        target_type="bg_invocation", target_id=str(inv.id),
        metadata_json={"bg_number": bg.bg_number, "document_id": doc.id}, created_at=sign_at,
    ))
    _bump("AuditLog")
    if stage == "signed_uploaded":
        inv.ceo_approval_token = _fake_token()   # CEO approval email dispatched, awaiting click
        return inv

    if stage == "on_hold":
        tc_head_user = pick(_role_holders(users_by_role_sap, "tc_head", sap_code))
        hold_at = clock.advance(hours=rng.uniform(1, 20))
        inv.stage = "on_hold"
        inv.hold_requested_by = tc_head_user.id if tc_head_user else None
        inv.ceo_approval_token = _fake_token()          # main send-gate approval, still pending
        inv.hold_cfo_approval_token = _fake_token()     # hold itself awaiting CFO-then-CEO sign-off
        db.session.add(WorkflowHistory(
            bank_guarantee_id=bg.id, from_stage="signed_uploaded", to_stage="on_hold",
            action=WorkflowAction.invocation_hold_requested.value, actor_id=tc_head_user.id if tc_head_user else None,
            actor_role="tc_head", comments="TC Head requested a hold pending vendor discussion.",
            created_at=hold_at,
        ))
        _bump("WorkflowHistory")
        db.session.add(AuditLog(
            event_type="invocation_hold_requested", actor_id=tc_head_user.id if tc_head_user else None,
            target_type="bg_invocation", target_id=str(inv.id), metadata_json={"bg_number": bg.bg_number},
            created_at=hold_at,
        ))
        _bump("AuditLog")
        return inv

    # Dual-gate send (14.8): both signed_document_id and ceo_approved_at must
    # be set, and stage must not be on_hold, before sent_to_bank_at is set.
    ceo_at = clock.advance(hours=rng.uniform(6, 60))
    inv.ceo_approved_at = ceo_at
    inv.ceo_approval_token = None
    sent_at = clock.advance(minutes=rng.randint(5, 60))
    inv.sent_to_bank_at = sent_at
    inv.stage = "sent_to_bank"
    bg.status = "submitted_to_bank"
    bg.current_stage = "submitted_to_bank"
    bg.updated_at = sent_at
    db.session.add(CeleryJob(task_name="invocation.notify_and_dispatch_send", status="completed",
                              related_bg_id=bg.id, created_at=sent_at,
                              completed_at=sent_at + timedelta(seconds=rng.randint(2, 30))))
    _bump("CeleryJob")
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="signed_uploaded", to_stage="sent_to_bank",
        action=WorkflowAction.invocation_sent_to_bank.value, actor_role="system",
        comments="Both gates cleared (signed letter + CEO approval); dispatched to the bank.",
        created_at=sent_at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="invocation_sent_to_bank", target_type="bg_invocation", target_id=str(inv.id),
        metadata_json={"bg_number": bg.bg_number}, created_at=sent_at,
    ))
    _bump("AuditLog")
    seen = set()
    for role in ("creator", "coordinator", "bu_fc"):
        for u in _role_holders(users_by_role_sap, role, sap_code):
            if u.id in seen:
                continue
            seen.add(u.id)
            _notify(u, "invocation_sent_to_bank", "Invocation claim sent to bank",
                    f"The invocation for {bg.bg_number} has cleared both gates and is now finalized.",
                    f"/bg/{bg.id}", sent_at, triggered_by_id=None)
    return inv


# --------------------------------------------------------------------------
# 12. Extension  (Section 4 #11; Section 9.2)
# --------------------------------------------------------------------------

def _do_extension(bg, clock, coordinator, stage):
    """stage in {"not_started", "requested", "uploaded"}. Returns the
    ExtensionRequest; when stage == "uploaded" the caller still needs to
    build the child BG and call `_link_extension_upload`."""
    req = ExtensionRequest(parent_bg_id=bg.id, stage="not_started",
                            coordinator_id=coordinator.id if coordinator else None, created_at=clock.now())
    db.session.add(req)
    db.session.flush()
    _bump("ExtensionRequest")
    if stage == "not_started":
        req.is_overdue = (bg.expiry_date - NOW.date()).days < 21
        return req

    vendor_slug = (bg.vendor_name or "vendor").split()[0].lower()
    vendor_email = f"procurement{rng.randint(100, 999)}@{vendor_slug}.example.com"
    at = clock.advance(days=rng.uniform(0.5, 3))
    req.vendor_email = vendor_email
    req.stage = "requested"
    req.requested_at = at
    req.is_overdue = (bg.expiry_date - NOW.date()).days < 21
    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage="live", to_stage="extension_requested",
        action=WorkflowAction.extension_requested.value, actor_id=coordinator.id if coordinator else None,
        actor_role="coordinator", comments="Extension requested from the vendor ahead of expiry.",
        created_at=at,
    ))
    _bump("WorkflowHistory")
    db.session.add(AuditLog(
        event_type="extension_requested", actor_id=coordinator.id if coordinator else None,
        target_type="bank_guarantee", target_id=str(bg.id),
        metadata_json={"bg_number": bg.bg_number, "vendor_email": vendor_email}, created_at=at,
    ))
    _bump("AuditLog")
    return req


def _link_extension_upload(parent_bg, child_bg, req, clock):
    at = clock.advance(hours=rng.uniform(2, 24))
    if at < child_bg.created_at:
        at = child_bg.created_at + timedelta(minutes=5)
    req.stage = "uploaded"
    req.child_bg_id = child_bg.id
    db.session.add(WorkflowHistory(
        bank_guarantee_id=parent_bg.id, from_stage="extension_requested", to_stage="extension_uploaded",
        action=WorkflowAction.extension_uploaded.value,
        actor_id=(child_bg.coordinator_id or child_bg.creator_id), actor_role="coordinator",
        comments=f"Extended Bank Guarantee {child_bg.bg_number} uploaded and linked to this record.",
        created_at=at,
    ))
    _bump("WorkflowHistory")


# --------------------------------------------------------------------------
# 13. Comparison report  (Section 4 #8; 14.7)
# --------------------------------------------------------------------------

def _maybe_comparison_report(bg, clock, actor, prob=0.8):
    if actor is None or not maybe(prob):
        return
    at = clock.advance(hours=rng.uniform(1, 36))
    row = GeneratedDocument(bank_guarantee_id=bg.id, document_kind="comparison_report",
                             storage_path=f"generated/comparison_{bg.id}_v1.pdf", file_format="pdf",
                             generated_by_user_id=actor.id, version=1, generated_at=at)
    db.session.add(row)
    _bump("GeneratedDocument")
    db.session.add(AuditLog(event_type="comparison_report_generated", actor_id=actor.id,
                             target_type="bank_guarantee", target_id=str(bg.id),
                             metadata_json={"bg_number": bg.bg_number, "version": 1}, created_at=at))
    _bump("AuditLog")
    if maybe(0.18):
        at2 = clock.advance(hours=rng.uniform(6, 60))
        row2 = GeneratedDocument(bank_guarantee_id=bg.id, document_kind="comparison_report",
                                  storage_path=f"generated/comparison_{bg.id}_v2.pdf", file_format="pdf",
                                  generated_by_user_id=actor.id, version=2, generated_at=at2)
        db.session.add(row2)
        _bump("GeneratedDocument")
        db.session.add(AuditLog(event_type="comparison_report_generated", actor_id=actor.id,
                                 target_type="bank_guarantee", target_id=str(bg.id),
                                 metadata_json={"bg_number": bg.bg_number, "version": 2}, created_at=at2))
        _bump("AuditLog")


# --------------------------------------------------------------------------
# 14. Main Bank Guarantee population loop  (Section 4 #4; Section 5 cohort
#     design -- see SEED_DATA_PLAN.md for the exact cohort-size rationale)
# --------------------------------------------------------------------------

SAP_CODES = ["GRP001", "INFRA001", "MFG001", "SRV001"]


def _po_pool_split(vendor_map):
    clean, open_ = [], []
    for vendor, pos in vendor_map.items():
        for p in pos:
            (clean if (p.is_executed and p.open_advance_amount is None) else open_).append((vendor, p))
    return clean, open_


def _pick_po_for_closure(clean_pool, open_pool, want_standard, bg_type):
    if want_standard:
        candidates = clean_pool if bg_type == "abg" else (clean_pool + [e for e in open_pool if e[1].is_executed])
    else:
        candidates = open_pool if bg_type == "abg" else ([e for e in open_pool if not e[1].is_executed] or open_pool)
    if not candidates:
        candidates = clean_pool + open_pool
    vendor, po = pick(candidates)
    return vendor, [po]


def _pick_sap():
    return weighted_pick([("GRP001", 0.20), ("INFRA001", 0.29), ("MFG001", 0.29), ("SRV001", 0.22)])


def _pick_bg_type():
    return weighted_pick([("pbg", 0.45), ("abg", 0.22), ("cpbg", 0.15), ("cpbg_cum_pbg", 0.08), ("cg", 0.10)])


def _creator_for(users_by_role_sap, sap_code):
    cands = _role_holders(users_by_role_sap, "creator", sap_code)
    return pick(cands) if cands else None


def _coordinator_for(users_by_role_sap, sap_code):
    cands = _role_holders(users_by_role_sap, "coordinator", sap_code)
    return pick(cands) if cands else None


def _spawn_bg(users_by_role_sap, vendor_map, sap_by_code, depth, po_override=None, bg_type_override=None,
              days_to_expiry=None, parent_bg_id=None):
    """Build one BankGuarantee shell + run its intake pipeline. Returns
    (bg, clock, requires_ceo_cfo, deviations, po_records, sap_code, creator, coordinator)."""
    sap_code = _pick_sap()
    sap = sap_by_code[sap_code]
    creator = _creator_for(users_by_role_sap, sap_code)
    coordinator = _coordinator_for(users_by_role_sap, sap_code)
    bg_type = bg_type_override or _pick_bg_type()
    format_variant = weighted_pick([("supply", 0.6), ("service", 0.4)])
    expenditure_type = weighted_pick([("opex", 0.55), ("capex", 0.45)])

    if po_override:
        vendor, po_records = po_override
    else:
        vendor, po_records = _pick_vendor_and_pos(vendor_map, max_pos=2)
    amount = _pick_amount(bg_type, po_records)
    currency = _pick_currency()
    if currency != "INR":
        currency = "INR"  # PO values are INR-denominated; keep FX BGs to the handful created separately
    issuing_bank = pick(ISSUING_BANKS)

    created_at = _depth_created_at(depth)
    issue_date, expiry_date, claim_expiry_date = _gen_dates(bg_type, created_at, days_to_expiry=days_to_expiry)

    bg = _new_bg_shell(creator, coordinator, sap, bg_type, format_variant, expenditure_type, vendor,
                        po_records, amount, currency, issuing_bank, issue_date, expiry_date,
                        claim_expiry_date, created_at, parent_bg_id=parent_bg_id)

    clock = Clock(created_at)
    requires_ceo_cfo = maybe(0.32)
    n_deviations = weighted_pick([(2, 0.35), (3, 0.3), (4, 0.2), (5, 0.1), (6, 0.05)])
    decided_by = [u for u in (_role_holders(users_by_role_sap, "buyer", sap_code)
                              + _role_holders(users_by_role_sap, "tc_head", sap_code)) if u]
    if not decided_by:
        decided_by = [creator] if creator else []

    deviations, _doc = _simulate_intake_pipeline(bg, creator, sap_code, clock, bool(parent_bg_id),
                                                  po_records, requires_ceo_cfo, decided_by, n_deviations)
    return bg, clock, requires_ceo_cfo, deviations, po_records, sap_code, creator, coordinator


def seed_bank_guarantees(users, vendor_map, echo=None):
    sap_by_code = {s.code: s for s in SapSystem.query.all()}
    users_by_role_sap = users["by_role_sap"]
    clean_pool, open_pool = _po_pool_split(vendor_map)

    all_root_bgs = []      # (bg, clock, sap_code, creator, coordinator) for post-processing
    extension_pairs = []   # (parent_bg, req, clock) needing a child spawned
    invocation_candidates = []   # (bg, clock, sap_code, stage)
    closure_candidates = []      # (bg, clock, sap_code, po_records, outcome)
    live_plain = []               # (bg, clock, sap_code) plain-live, for comparison reports / stray returns

    # ---- Cohort 1: draft-only (never submitted) ----
    for _ in range(13):
        bg, clock, req_ceo, deviations, po_records, sap_code, creator, coordinator = _spawn_bg(
            users_by_role_sap, vendor_map, sap_by_code, depth="shallow")
        if maybe(0.5):
            db.session.add(AuditLog(event_type="bg_draft_saved", actor_id=creator.id if creator else None,
                                     target_type="bank_guarantee", target_id=str(bg.id),
                                     metadata_json={"bg_number": bg.bg_number}, created_at=clock.now()))
            _bump("AuditLog")
        all_root_bgs.append((bg, clock, sap_code, creator, coordinator))

    # ---- Cohort 2: rejected ----
    reject_points = (["buyer"] * 6 + ["tc_head"] * 5 + ["finance"] * 2)
    rng.shuffle(reject_points)
    for reject_at in reject_points:
        bg, clock, req_ceo, deviations, po_records, sap_code, creator, coordinator = _spawn_bg(
            users_by_role_sap, vendor_map, sap_by_code, depth="rejected")
        _run_doa_chain(bg, creator, sap_code, users_by_role_sap, clock, req_ceo, bg.expenditure_type,
                        target="live", reject_at=reject_at)
        all_root_bgs.append((bg, clock, sap_code, creator, coordinator))

    # ---- Cohort 3: currently in-flight (pending at some stage right now) ----
    in_flight_targets = ["pending_buyer", "pending_category_lead", "pending_category_lead",
                          "pending_finance", "pending_finance", "pending_abex"]
    for target in in_flight_targets:
        bg, clock, req_ceo, deviations, po_records, sap_code, creator, coordinator = _spawn_bg(
            users_by_role_sap, vendor_map, sap_by_code, depth="shallow")
        _run_doa_chain(bg, creator, sap_code, users_by_role_sap, clock, req_ceo, bg.expenditure_type,
                        target=target)
        all_root_bgs.append((bg, clock, sap_code, creator, coordinator))
        _maybe_comparison_report(bg, clock, pick(_role_holders(users_by_role_sap, "tc_head", sap_code)) or creator, prob=0.3)

    if echo:
        echo(f"Seed: bank guarantees -- draft/rejected/in-flight cohorts done ({len(all_root_bgs)} so far).")

    db.session.commit()
    return {
        "sap_by_code": sap_by_code, "users_by_role_sap": users_by_role_sap,
        "clean_pool": clean_pool, "open_pool": open_pool,
        "all_root_bgs": all_root_bgs, "extension_pairs": extension_pairs,
        "invocation_candidates": invocation_candidates, "closure_candidates": closure_candidates,
        "live_plain": live_plain,
    }


def seed_live_cohorts(ctx, echo=None):
    """Cohorts 4-7: plain-live, extension-zone, invocation-zone,
    closure-zone -- every BG here reaches (at least once) BGStatus.live."""
    sap_by_code = ctx["sap_by_code"]
    users_by_role_sap = ctx["users_by_role_sap"]
    clean_pool, open_pool = ctx["clean_pool"], ctx["open_pool"]
    vendor_map = {}
    for v, p in clean_pool + open_pool:
        vendor_map.setdefault(v, []).append(p)

    live_bgs = []          # (bg, clock, sap_code, creator, coordinator)

    # ---- Cohort 4: plain live (deep future expiry, nothing special yet) ----
    for _ in range(16):
        bg, clock, req_ceo, deviations, po_records, sap_code, creator, coordinator = _spawn_bg(
            users_by_role_sap, vendor_map, sap_by_code, depth="medium")
        _run_doa_chain(bg, creator, sap_code, users_by_role_sap, clock, req_ceo, bg.expenditure_type, target="live")
        live_bgs.append((bg, clock, sap_code, creator, coordinator))
        _maybe_comparison_report(bg, clock, pick(_role_holders(users_by_role_sap, "abex", sap_code)) or creator)

    # ---- Cohort 5: extension zone (expiry within warning/overdue window) ----
    ext_stage_plan = (["not_started"] * 3 + ["requested"] * 4 + ["uploaded"] * 12)
    rng.shuffle(ext_stage_plan)
    child_specs = []
    for ext_stage in ext_stage_plan:
        days_to_expiry = rng.randint(-10, 44)   # some already overdue, most within the 45-day warning window
        bg, clock, req_ceo, deviations, po_records, sap_code, creator, coordinator = _spawn_bg(
            users_by_role_sap, vendor_map, sap_by_code, depth="deep", days_to_expiry=days_to_expiry)
        _run_doa_chain(bg, creator, sap_code, users_by_role_sap, clock, req_ceo, bg.expenditure_type, target="live")
        live_bgs.append((bg, clock, sap_code, creator, coordinator))
        _maybe_comparison_report(bg, clock, pick(_role_holders(users_by_role_sap, "tc_head", sap_code)) or creator)
        req = _do_extension(bg, clock, coordinator, ext_stage)
        if ext_stage == "uploaded":
            child_specs.append((bg, req, clock, sap_code))

    # Spawn the extension-child BGs now (each a full BG in its own right).
    for parent_bg, req, parent_clock, sap_code in child_specs:
        vendor = parent_bg.vendor_name
        po_records = [p for p in vendor_map.get(vendor, []) if p.po_number in (parent_bg.po_numbers or [])] \
            or _pick_vendor_and_pos(vendor_map)[1]
        child_bg, child_clock, child_req_ceo, child_deviations, child_pos, child_sap, child_creator, child_coord = \
            _spawn_bg(users_by_role_sap, vendor_map, sap_by_code, depth="deep",
                       po_override=(vendor, po_records), bg_type_override=parent_bg.bg_type,
                       parent_bg_id=parent_bg.id)
        child_target = weighted_pick([("live", 0.8), ("pending_abex", 0.1), ("pending_finance", 0.1)])
        _run_doa_chain(child_bg, child_creator, child_sap, users_by_role_sap, child_clock, child_req_ceo,
                        child_bg.expenditure_type, target=child_target)
        _link_extension_upload(parent_bg, child_bg, req, parent_clock)
        if child_target == "live":
            live_bgs.append((child_bg, child_clock, child_sap, child_creator, child_coord))
        else:
            ctx["all_root_bgs"].append((child_bg, child_clock, child_sap, child_creator, child_coord))

    # ---- Cohort 6: invocation zone (claim window approaching/critical) ----
    inv_stage_plan = (["approaching_window"] * 6 + ["critical"] * 3 + ["draft_generated"] * 4
                       + ["signed_uploaded"] * 3 + ["on_hold"] * 1 + ["sent_to_bank"] * 4)
    rng.shuffle(inv_stage_plan)
    for inv_stage in inv_stage_plan:
        days_to_claim = rng.randint(-3, 55) if inv_stage != "critical" else rng.randint(1, 13)
        bg, clock, req_ceo, deviations, po_records, sap_code, creator, coordinator = _spawn_bg(
            users_by_role_sap, vendor_map, sap_by_code, depth="deep", days_to_expiry=max(days_to_claim - 30, -40))
        bg.claim_expiry_date = NOW.date() + timedelta(days=days_to_claim)
        _run_doa_chain(bg, creator, sap_code, users_by_role_sap, clock, req_ceo, bg.expenditure_type, target="live")
        live_bgs.append((bg, clock, sap_code, creator, coordinator))
        _maybe_comparison_report(bg, clock, pick(_role_holders(users_by_role_sap, "bu_fc", sap_code)
                                                  + _role_holders(users_by_role_sap, "bu_cfmc", sap_code)) or creator,
                                  prob=0.5)
        _do_invocation(bg, clock, users_by_role_sap, sap_code, inv_stage)

    if echo:
        echo(f"Seed: bank guarantees -- plain-live/extension/invocation cohorts done "
             f"({len(live_bgs)} live so far, {len(child_specs)} extension children spawned).")

    # ---- Cohort 7: closure zone ----
    closure_outcomes = (["standard_closed"] * 12 + ["exception_closed"] * 5 + ["exception_offline_closed"] * 3
                         + ["exception_in_progress_cfo"] * 2 + ["exception_in_progress_ceo"] * 1
                         + ["exception_declined_ceo"] * 1 + ["tc_rejected"] * 2)
    rng.shuffle(closure_outcomes)
    return_needs = []   # (bg, clock, sap_code) fully-closed BGs eligible for a return
    for outcome in closure_outcomes:
        want_standard = outcome == "standard_closed"
        bg_type = _pick_bg_type()
        vendor, po_records = _pick_po_for_closure(clean_pool, open_pool, want_standard, bg_type)
        bg, clock, req_ceo, deviations, po_records, sap_code, creator, coordinator = _spawn_bg(
            users_by_role_sap, vendor_map, sap_by_code, depth="deep",
            po_override=(vendor, po_records), bg_type_override=bg_type)
        _run_doa_chain(bg, creator, sap_code, users_by_role_sap, clock, req_ceo, bg.expenditure_type, target="live")
        live_bgs.append((bg, clock, sap_code, creator, coordinator))
        _maybe_comparison_report(bg, clock, pick(_role_holders(users_by_role_sap, "abex", sap_code)) or creator)
        _do_closure(bg, clock, users_by_role_sap, sap_code, po_records, outcome)
        if bg.status == "closed":
            return_needs.append((bg, clock, sap_code))

    if echo:
        echo(f"Seed: bank guarantees -- closure cohort done ({len(closure_outcomes)} closures).")

    # A couple of independent returns on still-live BGs, for variety.
    already_returning = {bg.id for bg, _clock, _sap in return_needs}
    live_only_pool = [t for t in live_bgs if t[0].id not in already_returning]
    for bg, clock, sap_code, creator, coordinator in rng.sample(live_only_pool, k=min(3, len(live_only_pool))):
        if maybe(0.5):
            return_needs.append((bg, clock, sap_code))

    for bg, clock, sap_code in return_needs:
        if not maybe(0.8):
            continue
        stage = weighted_pick([("requested", 0.15), ("dispatched", 0.2), ("receipt_confirmed", 0.65)])
        _do_return(bg, clock, users_by_role_sap, sap_code, stage)

    ctx["live_bgs"] = live_bgs
    db.session.commit()
    if echo:
        echo(f"Seed: returns processed ({len(return_needs)} candidates).")
    return live_bgs


# --------------------------------------------------------------------------
# 15. Bank verification pass over every BG that ever went Live
# --------------------------------------------------------------------------

def seed_bank_verifications(ctx, echo=None):
    users_by_role_sap = ctx["users_by_role_sap"]
    n = 0
    for bg, clock, sap_code, creator, coordinator in ctx.get("live_bgs", []):
        _do_bank_verification(bg, clock, users_by_role_sap, sap_code)
        n += 1
    db.session.commit()
    if echo:
        echo(f"Seed: bank verifications created ({n}).")
    return n


# --------------------------------------------------------------------------
# 16. Saved views  (Section 4 #18)
# --------------------------------------------------------------------------

APPROVAL_QUEUE_FILTER_PRESETS = [
    {"name": "My urgent queue", "filter_state": {"status": ["pending_buyer_approval", "pending_category_lead_approval"], "sort": "oldest_first"}},
    {"name": "High value only", "filter_state": {"min_amount": 5000000, "sort": "amount_desc"}},
    {"name": "Elevated risk", "filter_state": {"tier": ["high", "prohibited"]}},
    {"name": "This week", "filter_state": {"created_within_days": 7}},
]
STATUS_HUB_FILTER_PRESETS = [
    {"name": "Expiring soon", "filter_state": {"expiry_within_days": 45}},
    {"name": "My vendor watchlist", "filter_state": {"vendor_names": ["Larsen & Toubro Ltd", "Tata Projects Limited"]}},
    {"name": "Closed this quarter", "filter_state": {"status": ["closed"], "period": "quarter"}},
]


def seed_saved_views(users, echo=None):
    queue_roles = {"buyer", "tc_head", "bu_fc", "bu_cfmc", "abex"}
    candidates = [u for u in users["all"] if set(u.granted_roles or []) & queue_roles]
    n = 0
    for user in candidates:
        if not maybe(0.55):
            continue
        preset = pick(APPROVAL_QUEUE_FILTER_PRESETS)
        at = days_ago(rng.randint(2, 70), hour=rng.randint(8, 19))
        db.session.add(SavedView(user_id=user.id, page_key="approval_queue", name=preset["name"],
                                  filter_state=preset["filter_state"], created_at=at))
        _bump("SavedView")
        db.session.add(AuditLog(event_type="queue_view_saved", actor_id=user.id, target_type="user",
                                 target_id=str(user.id), metadata_json={"name": preset["name"]}, created_at=at))
        _bump("AuditLog")
        n += 1
        if maybe(0.3):
            preset2 = pick(STATUS_HUB_FILTER_PRESETS)
            at2 = days_ago(rng.randint(1, 60), hour=rng.randint(8, 19))
            db.session.add(SavedView(user_id=user.id, page_key="status_hub", name=preset2["name"],
                                      filter_state=preset2["filter_state"], created_at=at2))
            _bump("SavedView")
            db.session.add(AuditLog(event_type="status_hub_view_saved", actor_id=user.id, target_type="user",
                                     target_id=str(user.id), metadata_json={"name": preset2["name"]}, created_at=at2))
            _bump("AuditLog")
            n += 1
    db.session.commit()
    if echo:
        echo(f"Seed: saved views created ({n}).")
    return n


# --------------------------------------------------------------------------
# 17. Policy assistant conversations  (Section 4 #23)
# --------------------------------------------------------------------------

ASSISTANT_THREADS = [
    [
        ("Why did this guarantee get routed to CEO/CFO sign-off instead of going straight to ABEX?",
         "A Bank Guarantee is routed to CEO/CFO sign-off whenever the highest effective deviation tier "
         "across its clauses is 'high' or 'prohibited'. If every deviation on the guarantee is 'low' tier, "
         "the Finance stage forwards it directly to ABEX Verification instead.",
         ["doa_matrix.ceo_cfo_condition"]),
        ("What counts as a prohibited clause?",
         "Three patterns are currently flagged as prohibited: unlimited liability wording, an arbitration "
         "seat outside India, and a governing-law clause naming a foreign jurisdiction. Any clause matching "
         "one of these is forced to 'prohibited' tier regardless of what the AI initially proposed, and it "
         "blocks progress until an administrator records an override.",
         ["prohibited_clause_patterns"]),
    ],
    [
        ("क्या एक्सटेंशन रिक्वेस्ट भेजने की कोई समय-सीमा है?",
         "जी हाँ। एक्सपायरी से 45 दिन पहले guarantee 'warning' zone में आ जाती है, और 21 दिन से कम रहने पर "
         "इसे overdue माना जाता है। कोऑर्डिनेटर को समय रहते vendor को एक्सटेंशन रिक्वेस्ट भेज देनी चाहिए।",
         ["extension_policy"]),
    ],
    [
        ("How is the claim window for invocation different from the guarantee's expiry date?",
         "The expiry date is when the guarantee itself lapses. The claim window is the additional period "
         "after that during which we can still invoke it against the bank -- tracked separately as the "
         "claim_expiry_date. The system flags a guarantee as 'approaching' 60 days before that date and "
         "'critical' inside 14 days.",
         ["invocation_policy"]),
        ("What happens if the TC Head puts an invocation on hold?",
         "The invocation moves to an 'on_hold' stage and the send process is paused. When the hold is later "
         "released, the record returns to whichever stage it should logically be in based on whether a "
         "signed letter has already been uploaded -- it never just resumes from a stored 'previous stage'.",
         ["invocation_policy"]),
    ],
    [
        ("Why can't I both review and verify the closure of the same guarantee?",
         "Closure verification enforces segregation of duties: the ABEX user who verifies a closure must be "
         "different from whoever initiated it and, for exception closures, different from whoever performed "
         "the category-lead review as well. This is by design and isn't configurable per-user.",
         ["closure_policy"]),
        ("What's the difference between a standard closure and an exception closure?",
         "A closure is 'standard' when the underlying PO is fully executed and, for an ABG, the open advance "
         "is zero -- it goes straight to ABEX verification. Otherwise it's an 'exception' and needs a "
         "category-lead review plus sequential CFO-then-CEO sign-off before ABEX can verify it.",
         ["closure_policy"]),
    ],
    [
        ("An ABG intake is being blocked with a shortfall error -- what does that mean?",
         "For an Advance Bank Guarantee, the guaranteed amount must be at least the total open advance still "
         "outstanding on the referenced purchase order(s). If it's lower, intake blocks the submission with a "
         "shortfall error -- there's no override for this one, the amount has to be corrected.",
         ["intake_policy"]),
    ],
    [
        ("Why is my Bank Guarantee's issuing bank showing as unresolved on the verification page?",
         "Bank verification looks up a contact email for the issuing bank from the approved bank list. If the "
         "guarantee's issuing bank doesn't have a configured contact, verification falls back to a 'not sent' "
         "state instead of a normal pending one -- worth checking the bank name matches our approved list "
         "exactly.",
         ["approved_banks"]),
    ],
    [
        ("How many people need to review a Bank Guarantee before it goes live?",
         "At minimum: the Buyer, the Category Lead (TC Head), and the relevant Finance reviewer (BU FC for "
         "opex, BU CFMC for capex), followed by ABEX Verification. If the deviation tier is high or "
         "prohibited, CEO/CFO sign-off is inserted between Finance and ABEX.",
         ["doa_matrix"]),
        ("Can a Buyer also review their own submission at the Category Lead stage?",
         "Nothing technically stops the same person from holding both roles, but for proper separation of "
         "duties each SAP system should be staffed with distinct people in the Buyer, TC Head, Finance and "
         "ABEX queues wherever possible.",
         None),
    ],
    [
        ("What's the difference between CPBG and CPBG cum PBG?",
         "A CPBG (Composite Performance Bank Guarantee) covers performance obligations only. A CPBG cum PBG "
         "combines the composite performance cover with a standard performance guarantee in a single "
         "instrument, so it's used when both obligations need to be secured under one guarantee.",
         None),
    ],
    [
        ("मुझे कैसे पता चलेगा कि किसी guarantee पर deviation का असर क्या है?",
         "हर deviation पर एक effective tier दिखता है -- low, high या prohibited। यही tier तय करता है कि "
         "guarantee को आगे बढ़ने के लिए एक्स्ट्रा (CEO/CFO) मंज़ूरी चाहिए या नहीं। आप BG की detail page पर "
         "पूरी deviation list और AI की व्याख्या देख सकते हैं।",
         ["doa_matrix.ceo_cfo_condition"]),
    ],
    [
        ("What documents get generated when I click 'Generate Invocation Draft'?",
         "Two files are produced together, sharing the same version number -- an editable Word (.docx) draft "
         "and a PDF rendering of the same content. The Word version is what gets linked as the working draft; "
         "the PDF is a read-only copy for circulation.",
         ["invocation_policy"]),
    ],
    [
        ("Does the comparison report use the same AI model as the intake extraction?",
         "Yes, template clause comparison and the original extraction both run on the same Gemini model "
         "configured for this deployment. The comparison report itself is generated as a single PDF, "
         "versioned per Bank Guarantee, separate from the invocation letter documents.",
         None),
    ],
    [
        ("If a vendor's guarantee lists two purchase orders, do they need to be from the same vendor?",
         "Yes -- every purchase order referenced by a single Bank Guarantee must belong to the same vendor, "
         "and that vendor should match the guarantee's own vendor name. Mixing PO numbers from different "
         "vendors on one guarantee isn't supported.",
         ["intake_policy"]),
    ],
    [
        ("What's the executive approval expiry window if the CEO doesn't respond?",
         "Executive sign-off links (for elevated deviations, closures, or invocations) are time-boxed; if "
         "the configured expiry window passes without a click, the link stops working and the request needs "
         "to be re-issued.",
         ["executive_approval_expiry_hours"]),
    ],
    [
        ("Why does my saved Approval Queue filter not show guarantees from other business units?",
         "Visibility is scoped to your own SAP system unless you hold the admin role. A saved filter only "
         "ever searches within the guarantees you're already allowed to see, so guarantees from another SAP "
         "system won't appear even if they'd otherwise match your filter.",
         None),
    ],
]


def seed_assistant_conversations(users, echo=None):
    queue_roles = {"buyer", "tc_head", "bu_fc", "bu_cfmc", "abex", "coordinator", "creator"}
    candidates = [u for u in users["all"] if set(u.granted_roles or []) & queue_roles]
    threads = list(ASSISTANT_THREADS)
    rng.shuffle(threads)
    n_msgs = 0
    for thread in threads:
        user = pick(candidates)
        session_start = days_ago(rng.randint(0, 75), hour=rng.randint(8, 21))
        t = session_start
        for question, answer, sources in thread:
            t = t + timedelta(minutes=rng.randint(1, 4))
            if t > NOW:
                t = NOW
            db.session.add(AssistantMessage(user_id=user.id, role="user", content=question, created_at=t))
            _bump("AssistantMessage")
            n_msgs += 1
            t = t + timedelta(seconds=rng.randint(4, 20))
            if t > NOW:
                t = NOW
            db.session.add(AssistantMessage(user_id=user.id, role="assistant", content=answer,
                                             cited_sources=sources, created_at=t))
            _bump("AssistantMessage")
            db.session.flush()
            db.session.add(AiInteraction(feature="policy_assistant", user_id=user.id,
                                          model_version="gemini-2.5-flash",
                                          prompt_token_count=rng.randint(600, 1800),
                                          response_token_count=rng.randint(80, 260),
                                          latency_ms=rng.randint(900, 3000), status="success", created_at=t))
            _bump("AiInteraction")
            n_msgs += 1
    db.session.commit()
    if echo:
        echo(f"Seed: assistant conversations created ({len(threads)} threads, {n_msgs} messages).")
    return n_msgs


# --------------------------------------------------------------------------
# 18. Scheduled maintenance jobs  (Section 6's sampling rule; Section 11.5)
# --------------------------------------------------------------------------

def seed_maintenance_jobs(window_days=85, recent_days=6, echo=None):
    n = 0
    # Full-window daily jobs (cheap enough to backfill in full).
    for d in range(window_days, -1, -1):
        day = days_ago(d)
        for task_name, hour in (("maintenance.daily_expiry_scan", 1),
                                 ("maintenance.daily_claim_window_scan", 2),
                                 ("maintenance.daily_extension_digest", 8)):
            started = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            if started > NOW:
                continue
            finished = started + timedelta(seconds=rng.randint(2, 40))
            if finished > NOW:
                finished = NOW
            db.session.add(CeleryJob(task_name=task_name, status="completed", created_at=started,
                                      completed_at=finished))
            n += 1
    db.session.commit()
    _bump("CeleryJob", n)

    # High-frequency jobs: only the most recent few days (Section 6).
    n2 = 0
    cutoff = NOW - timedelta(days=recent_days)
    cursor = cutoff.replace(minute=5, second=0, microsecond=0)
    if cursor < cutoff:
        cursor += timedelta(hours=1)
    while cursor <= NOW:
        finished = cursor + timedelta(seconds=rng.randint(1, 15))
        db.session.add(CeleryJob(task_name="maintenance.warm_dashboard_cache", status="completed",
                                  created_at=cursor, completed_at=min(finished, NOW)))
        n2 += 1
        cursor += timedelta(hours=1)

    cursor = cutoff.replace(minute=(0 if cutoff.minute < 30 else 30), second=0, microsecond=0)
    while cursor <= NOW:
        finished = cursor + timedelta(seconds=rng.randint(1, 10))
        db.session.add(CeleryJob(task_name="maintenance.bank_verification_poll", status="completed",
                                  created_at=cursor, completed_at=min(finished, NOW)))
        n2 += 1
        cursor += timedelta(minutes=30)

    db.session.commit()
    _bump("CeleryJob", n2)
    if echo:
        echo(f"Seed: maintenance CeleryJob rows created ({n} daily-cadence + {n2} sampled high-frequency).")
    return n + n2


# --------------------------------------------------------------------------
# 19. Auth activity  (login_success / login_failed / logout AuditLog rows --
#     these ARE the login history; there is no separate session table)
# --------------------------------------------------------------------------

SESSION_COUNT_BY_ARCHETYPE = {
    "power": (11, 16), "regular": (5, 9), "casual": (2, 4), "new": (1, 2),
    "dormant": (2, 3), "admin_staff": (10, 15), "new_unapproved": (0, 0), "rejected": (1, 1),
}


def seed_auth_activity(users, echo=None):
    n = 0
    persona_by_email = {p["email"] + f"@{COMPANY_DOMAIN}": p for p in PERSONAS}
    for user in users["all"]:
        latest_login = None
        persona = persona_by_email.get(user.email)
        archetype = persona["archetype"] if persona else "regular"
        lo, hi = SESSION_COUNT_BY_ARCHETYPE.get(archetype, (5, 9))
        sessions = rng.randint(lo, hi)
        if sessions == 0:
            continue
        window_start_days = max((NOW - user.created_at).days, 1)
        for _ in range(sessions):
            day_offset = rng.randint(0, window_start_days)
            at = days_ago(day_offset, hour=rng.randint(7, 22), minute=rng.randint(0, 59))
            if at < user.created_at:
                at = user.created_at + timedelta(hours=rng.randint(0, 6))
            if archetype == "rejected" and at > user.created_at + timedelta(days=3):
                at = user.created_at + timedelta(hours=rng.randint(1, 48))
            db.session.add(AuditLog(event_type="login_success", actor_id=user.id, target_type="user",
                                     target_id=str(user.id), metadata_json={"email": user.email},
                                     created_at=at))
            n += 1
            if latest_login is None or at > latest_login:
                latest_login = at
            if maybe(0.28):
                logout_at = at + timedelta(minutes=rng.randint(5, 240))
                if logout_at > NOW:
                    logout_at = NOW
                db.session.add(AuditLog(event_type="logout", actor_id=user.id, target_type="user",
                                         target_id=str(user.id), metadata_json={"email": user.email},
                                         created_at=logout_at))
                n += 1
        if archetype in ("rejected",) or maybe(0.15):
            fail_at = days_ago(rng.randint(1, max(window_start_days, 1)), hour=rng.randint(7, 22))
            db.session.add(AuditLog(event_type="login_failed", actor_id=None, target_type="user",
                                     target_id=str(user.id),
                                     metadata_json={"email": user.email, "reason": "invalid_password"},
                                     created_at=fail_at))
            n += 1
        if maybe(0.2) and archetype not in ("new_unapproved", "rejected"):
            at = days_ago(rng.randint(1, window_start_days), hour=rng.randint(9, 18))
            db.session.add(AuditLog(event_type="password_changed", actor_id=user.id, target_type="user",
                                     target_id=str(user.id), created_at=at))
            n += 1
        if maybe(0.3) and archetype not in ("new_unapproved", "rejected"):
            at = days_ago(rng.randint(1, window_start_days), hour=rng.randint(9, 18))
            db.session.add(AuditLog(event_type="preferences_updated", actor_id=user.id, target_type="user",
                                     target_id=str(user.id), created_at=at))
            n += 1
        if len(user.granted_roles or []) > 1 and maybe(0.4):
            at = days_ago(rng.randint(1, window_start_days), hour=rng.randint(9, 18))
            other_role = pick([r for r in user.granted_roles if r != user.active_role] or [user.active_role])
            db.session.add(AuditLog(event_type="role_switched", actor_id=user.id, target_type="user",
                                     target_id=str(user.id),
                                     metadata_json={"from": user.active_role, "to": other_role}, created_at=at))
            n += 1
        if maybe(0.25) and archetype not in ("new_unapproved",):
            at = days_ago(rng.randint(0, min(window_start_days, 20)), hour=rng.randint(9, 20))
            db.session.add(AuditLog(event_type="notifications_mark_all_read", actor_id=user.id,
                                     target_type="user", target_id=str(user.id), created_at=at))
            n += 1
        if latest_login is not None:
            user.last_login_at = latest_login   # keep this consistent with the real login trail above
    db.session.commit()
    _bump("AuditLog", n)
    if echo:
        echo(f"Seed: auth/profile activity AuditLog rows created ({n}).")
    return n


# --------------------------------------------------------------------------
# 20. Admin activity flourish  (Section 5's optional single settings edit)
# --------------------------------------------------------------------------

def seed_admin_flourish(users, echo=None):
    admin = users["by_email"].get(f"kabir.malhotra@{COMPANY_DOMAIN}") or users.get("admin")
    if admin is None:
        return
    setting = ApplicationSetting.query.filter_by(setting_key="extension_policy").first()
    if setting is None:
        return
    at = days_ago(rng.randint(3, 25), hour=rng.randint(9, 17))
    old_value = dict(setting.setting_value or {})
    new_value = dict(old_value)
    new_value["warning_days"] = old_value.get("warning_days", 45)
    new_value["overdue_days"] = old_value.get("overdue_days", 21)
    new_value["reminder_cc_emails"] = ["treasury.ops@bg.center"]
    setting.setting_value = new_value
    setting.version = (setting.version or 1) + 1
    setting.changed_by = admin.id
    setting.change_reason = "Added treasury.ops@bg.center to the extension reminder CC list for the quarterly audit."
    setting.updated_at = at
    db.session.add(AuditLog(event_type="config_changed", actor_id=admin.id, target_type="application_setting",
                             target_id=str(setting.id),
                             metadata_json={"setting_key": "extension_policy", "old_version": setting.version - 1,
                                            "new_version": setting.version,
                                            "change": "added treasury.ops@bg.center to reminder_cc_emails"},
                             created_at=at))
    _bump("AuditLog")

    at2 = days_ago(rng.randint(1, 20), hour=rng.randint(9, 17))
    db.session.add(AuditLog(event_type="business_units_updated", actor_id=admin.id, target_type="sap_system",
                             target_id=None,
                             metadata_json={"note": "Refreshed business-unit display names for the quarterly review."},
                             created_at=at2))
    _bump("AuditLog")
    db.session.commit()
    if echo:
        echo("Seed: admin activity flourish applied (1 settings version bump + business-unit touch).")


# --------------------------------------------------------------------------
# 21. "Looks alive right now" freshness touch  (Section 6 / 17.10)
# --------------------------------------------------------------------------

def seed_freshness_touch(ctx, echo=None):
    """Force this signal explicitly rather than leaving it to probabilistic
    sampling: a few guaranteed-unread very-recent notifications, and at
    least one Celery job still genuinely `processing`/`queued` rather than
    every job in the table showing `completed`."""
    touched_notif = 0
    recent_notifs = Notification.query.order_by(Notification.created_at.desc()).limit(12).all()
    for n in recent_notifs[:5]:
        n.is_read = False
        n.read_at = None
        touched_notif += 1

    live_bgs = ctx.get("live_bgs") or []
    all_recent = [t[0] for t in live_bgs] or [t[0] for t in ctx.get("all_root_bgs", [])]
    target_bg = max(all_recent, key=lambda b: b.created_at) if all_recent else None
    processing_jobs = 0
    if target_bg is not None:
        started = minutes_ago(rng.randint(1, 4))
        db.session.add(CeleryJob(task_name="po_sap_cross_check", status="processing", related_bg_id=target_bg.id,
                                  triggered_by=target_bg.creator_id, created_at=started, completed_at=None))
        processing_jobs += 1
        started2 = minutes_ago(rng.randint(0, 2))
        db.session.add(CeleryJob(task_name="maintenance.bank_verification_poll", status="queued",
                                  created_at=started2, completed_at=None))
        processing_jobs += 1
    db.session.commit()
    _bump("CeleryJob", processing_jobs)
    if echo:
        echo(f"Seed: freshness touch applied ({touched_notif} notifications forced unread, "
             f"{processing_jobs} in-flight CeleryJob rows).")


# --------------------------------------------------------------------------
# 22. Top-level orchestrator + summary
# --------------------------------------------------------------------------

def reset_demo_data(echo=None):
    """Delete exactly the rows this module owns, leaving the baseline seed
    data from `seed_service.py` untouched. Safe because `seed_service.py`
    never writes to any of the tables emptied in the first block below, and
    the User / SapPoRecord deletes are filtered to this module's own known
    natural keys rather than touched wholesale."""
    def _echo(msg):
        if echo:
            echo(msg)
        else:
            logger.info(msg)

    # Every row in these tables belongs to this module -- seed_service.py
    # never creates BankGuarantee rows or anything hanging off one.
    for model in (BgInvocation, BgClosure, BgReturn, ExtensionRequest, Dispatch,
                  GeneratedDocument, DocumentAnalysis, Document, Deviation, WorkflowHistory,
                  AiInteraction, BankVerification, CeleryJob, Notification, SavedView,
                  AssistantMessage, AuditLog, BankGuarantee):
        n = model.query.delete(synchronize_session=False)
        _echo(f"Reset: cleared {n} {model.__name__} row(s).")

    persona_emails = [f"{p['email']}@{COMPANY_DOMAIN}" for p in PERSONAS]
    persona_users = User.query.filter(User.email.in_(persona_emails)).all()
    persona_ids = [u.id for u in persona_users]
    if persona_ids:
        UserPreference.query.filter(UserPreference.user_id.in_(persona_ids)).delete(synchronize_session=False)
        User.query.filter(User.id.in_(persona_ids)).delete(synchronize_session=False)
        _echo(f"Reset: cleared {len(persona_ids)} persona User row(s) (baseline admin untouched).")

    new_po_numbers = [spec["po_number"] for spec in NEW_PO_RECORDS]
    n_po = SapPoRecord.query.filter(SapPoRecord.po_number.in_(new_po_numbers)).delete(synchronize_session=False)
    _echo(f"Reset: cleared {n_po} added SapPoRecord row(s) (original 8 untouched).")

    db.session.commit()
    _echo("Reset: complete. Run `flask seed-demo-data` again to regenerate.")


def seed_demo_data(echo=None, now=None, reset=False):
    """Entry point wired to `flask seed-demo-data`. Safe to run more than
    once: every step below is additive and keyed off natural uniqueness
    (email / po_number / bg_number), never touching what
    `seed_service.initialize_seed_data()` already created."""
    global NOW
    NOW = now or datetime.utcnow()
    COUNTS.clear()

    def _echo(msg):
        if echo:
            echo(msg)
        else:
            logger.info(msg)

    db.create_all()
    from bgcc.services.seed_service import (
        seed_admin_user,
        seed_purchase_orders,
        seed_sap_systems,
        seed_starter_settings,
    )
    seed_sap_systems(echo=_echo)
    seed_admin_user(echo=_echo)
    seed_starter_settings(echo=_echo)
    seed_purchase_orders(echo=_echo)

    if reset:
        reset_demo_data(echo=_echo)

    _BG_COUNTER[0] = BankGuarantee.query.count()

    if User.query.filter_by(email=f"priya.sharma@{COMPANY_DOMAIN}").first():
        _echo("Seed: demo dataset already present (found priya.sharma@bg.center) -- "
              "skipping to avoid duplicating a previous run. Run `flask seed-demo-data --reset` "
              "if you specifically want to wipe this module's rows and regenerate them.")
        return COUNTS

    _echo(f"Seed: starting demo data generation (NOW = {NOW.isoformat()}Z)...")
    users = seed_users(echo=_echo)
    vendor_map = seed_more_purchase_orders(echo=_echo)
    ctx = seed_bank_guarantees(users, vendor_map, echo=_echo)
    seed_live_cohorts(ctx, echo=_echo)
    seed_bank_verifications(ctx, echo=_echo)
    seed_saved_views(users, echo=_echo)
    seed_assistant_conversations(users, echo=_echo)
    seed_maintenance_jobs(echo=_echo)
    seed_auth_activity(users, echo=_echo)
    seed_admin_flourish(users, echo=_echo)
    seed_freshness_touch(ctx, echo=_echo)

    try:
        from bgcc.services import analytics_service
        analytics_service.warm_dashboard_cache()
        _echo("Seed: dashboard cache warmed.")
    except Exception as exc:  # pragma: no cover - cache warm is best-effort
        _echo(f"Seed: could not warm dashboard cache automatically ({exc}); "
              f"restarting the app will do it on next boot.")

    _echo("")
    _echo("Seed: demo data generation complete. Row counts created this run:")
    for model_name in sorted(COUNTS):
        _echo(f"  {model_name:<20} {COUNTS[model_name]}")
    return COUNTS