"""Delegation-of-authority (DoA) workflow engine.

Fully data-driven from `application_settings.doa_matrix`. Every stage-sequencing,
routing and deviation-visibility decision in the platform should go through the
functions here so the matrix is the single source of truth (configurable in
Step 7 without code changes).
"""
import copy
from bgcc.models.deviations import Deviation
from bgcc.models.enums import DeviationTier
from bgcc.models.settings import ApplicationSetting

ROLE_TO_STATUS = {
    "buyer": "pending_buyer_approval",
    "tc_head": "pending_category_lead_approval",
    "bu_fc": "pending_fc_approval",
    "bu_cfmc": "pending_bu_cfmc_approval",
    "abex": "pending_abex_verification",
}
STATUS_TO_ROLE = {v: k for k, v in ROLE_TO_STATUS.items()}

FINANCE_ROLES = {"bu_fc", "bu_cfmc"}
QUEUE_ROLES = set(ROLE_TO_STATUS.keys())
DOA_ROLES = {"creator", "buyer", "tc_head", "bu_fc", "bu_cfmc", "ceo_cfo", "abex"}

TIER_RANK = {
    DeviationTier.low.value: 0,
    DeviationTier.high.value: 1,
    DeviationTier.prohibited.value: 2,
    "low": 0,
    "high": 1,
    "prohibited": 2,
}

_DEFAULT_VISIBILITY = [
    DeviationTier.low.value,
    DeviationTier.high.value,
    DeviationTier.prohibited.value,
]

DEFAULT_STAGE_SEQUENCE = {
    "opex": ["buyer", "tc_head", "bu_fc", "ceo_cfo", "abex"],
    "capex": ["buyer", "tc_head", "bu_cfmc", "ceo_cfo", "abex"],
}

DEFAULT_CEO_CFO_CONDITION = {
    "on_tiers": ["high", "prohibited"]
}

DEFAULT_DEVIATION_VISIBILITY = {
    role: ["low", "high", "prohibited"] for role in DOA_ROLES
}

DEFAULT_DOA_MATRIX = {
    "stage_sequence": DEFAULT_STAGE_SEQUENCE,
    "ceo_cfo_condition": DEFAULT_CEO_CFO_CONDITION,
    "deviation_visibility": DEFAULT_DEVIATION_VISIBILITY,
}


def get_doa_matrix():
    setting = ApplicationSetting.query.filter_by(setting_key="doa_matrix").first()
    raw = (setting.setting_value if setting else {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    matrix = {
        "stage_sequence": dict(raw.get("stage_sequence") or DEFAULT_STAGE_SEQUENCE),
        "ceo_cfo_condition": dict(raw.get("ceo_cfo_condition") or DEFAULT_CEO_CFO_CONDITION),
        "deviation_visibility": dict(raw.get("deviation_visibility") or DEFAULT_DEVIATION_VISIBILITY),
    }
    seq = matrix["stage_sequence"]
    if not seq.get("opex"):
        seq["opex"] = list(DEFAULT_STAGE_SEQUENCE["opex"])
    if not seq.get("capex"):
        seq["capex"] = list(DEFAULT_STAGE_SEQUENCE["capex"])
    return matrix


def stage_sequence_for(bg):
    matrix = get_doa_matrix()
    seq = matrix.get("stage_sequence", {})
    exp = getattr(bg, "expenditure_type", None)
    exp_key = exp.value if hasattr(exp, "value") else str(exp).lower() if exp else None
    roles = seq.get(exp_key) if exp_key else None
    if not roles:
        roles = seq.get("opex") or seq.get("capex") or DEFAULT_STAGE_SEQUENCE["opex"]
    return list(roles)


def highest_tier(bg):
    if not bg or not bg.id:
        return None
    best = None
    for d in Deviation.query.filter_by(bank_guarantee_id=bg.id).all():
        raw_tier = getattr(d, "effective_tier", None)
        tier_str = raw_tier.value if hasattr(raw_tier, "value") else str(raw_tier) if raw_tier else None
        if tier_str:
            tier_str = tier_str.lower()
            if best is None or TIER_RANK.get(tier_str, -1) > TIER_RANK.get(best, -1):
                best = tier_str
    return best


def requires_ceo_cfo(bg):
    matrix = get_doa_matrix()
    raw_on_tiers = matrix.get("ceo_cfo_condition", {}).get("on_tiers") or DEFAULT_CEO_CFO_CONDITION["on_tiers"]
    on_tiers = set(t.value.lower() if hasattr(t, "value") else str(t).lower() for t in raw_on_tiers)
    ht = highest_tier(bg)
    ht_val = (ht.value if hasattr(ht, "value") else str(ht) if ht else "").lower()
    return ht_val in on_tiers


def full_sequence(bg):
    seq = stage_sequence_for(bg)
    if "ceo_cfo" in seq and not requires_ceo_cfo(bg):
        seq = [r for r in seq if r != "ceo_cfo"]
    return seq


def current_authorized_role(bg):
    """The role authorized at the BG's current stage (None for ceo_cfo/terminal)."""
    if not bg or not bg.status:
        return None
    st = bg.status.value if hasattr(bg.status, "value") else str(bg.status)
    return STATUS_TO_ROLE.get(st)


def next_role(bg):
    seq = full_sequence(bg)
    st = bg.status.value if hasattr(bg.status, "value") else str(bg.status)
    if st == "pending_ceo_cfo":
        # Creator advances the offline CEO/CFO stage; next is ABEX.
        return "abex" if "abex" in seq else None
    cur = current_authorized_role(bg)
    if cur is None:
        return None
    if cur in seq:
        idx = seq.index(cur)
        if idx + 1 < len(seq):
            return seq[idx + 1]
    return None


def is_final_stage(bg, role=None):
    cur = role or current_authorized_role(bg)
    if cur == "abex":
        return True
    seq = full_sequence(bg)
    if not seq or cur not in seq:
        return False
    return seq.index(cur) == len(seq) - 1


def stage_info(bg):
    seq = full_sequence(bg)
    cur_role = current_authorized_role(bg)
    st = bg.status.value if hasattr(bg.status, "value") else str(bg.status)
    if st == "pending_ceo_cfo":
        cur_role = "ceo_cfo"
    cur_idx = seq.index(cur_role) if cur_role in seq else -1
    stages = []
    for i, r in enumerate(seq):
        status_code = "pending_ceo_cfo" if r == "ceo_cfo" else ROLE_TO_STATUS.get(r, r)
        stages.append({
            "role": r,
            "label": role_label(r),
            "status_code": status_code,
            "order": i + 1,
            "is_current": (i == cur_idx),
            "is_completed": (cur_idx > i or st in ("live", "closed")),
            "is_upcoming": (cur_idx < i and st not in ("live", "closed")),
        })
    nxt = next_role(bg)
    return {
        "sequence": seq,
        "stages": stages,
        "current_role": cur_role,
        "current_index": cur_idx,
        "total_stages": len(seq),
        "next_role": nxt,
        "next_label": role_label(nxt) if nxt else ("Live Activation" if (cur_role == "abex" or is_final_stage(bg, cur_role)) else None),
        "is_final": (cur_role == "abex" or is_final_stage(bg, cur_role)),
    }


def visible_tiers(role):
    matrix = get_doa_matrix()
    vis = matrix.get("deviation_visibility", {})
    tiers = vis.get(role)
    return list(tiers) if tiers else list(_DEFAULT_VISIBILITY)


def visible_deviations(bg, role):
    """Deviations the given role is entitled to see, filtered at the data layer."""
    tiers = set(visible_tiers(role))
    devs = Deviation.query.filter_by(bank_guarantee_id=bg.id).order_by(Deviation.id).all()
    filtered = []
    for d in devs:
        eff = d.effective_tier.value if hasattr(d.effective_tier, "value") else str(d.effective_tier) if d.effective_tier else None
        if eff in tiers:
            filtered.append(d)
    return filtered


def queue_role_status(role):
    if role == "ceo_cfo":
        return "pending_ceo_cfo"
    return ROLE_TO_STATUS.get(role)


def role_label(role):
    if role == "tc_head":
        return "Category Lead (TC Head)"
    if role == "bu_fc":
        return "Finance (BU FC)"
    if role == "bu_cfmc":
        return "Finance (BU CFMC)"
    if role == "ceo_cfo":
        return "Elevated CEO / CFO"
    if role == "abex":
        return "ABEX Verification"
    if role == "buyer":
        return "Buyer"
    return (role or "").replace("_", " ").title()

