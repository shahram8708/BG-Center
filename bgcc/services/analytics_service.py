"""Portfolio analytics computation and cache warming (Step 6).

Computes, per active SAP system plus a company-wide aggregate, the portfolio
statistics the Dashboard reads.
"""
import logging
from bgcc.extensions import cache, db
from bgcc.models.bank_verifications import BankVerification
from bgcc.models.enums import BGStatus
from bgcc.models.reference import BankGuarantee, SapSystem

logger = logging.getLogger(__name__)


def _empty_aggregate():
    return {
        "sap_system_id": None,
        "sap_system_name": "Company-wide",
        "total_active_value": 0.0,
        "bank_confirmed_value": 0.0,
        "active_count": 0,
        "by_bank": {},
        "by_vendor": {},
        "by_business_unit": {},
        "by_bg_type": {},
        "has_data": False,
    }


def _add_to(acc, key, amount, count=1):
    entry = acc.setdefault(key, {"value": 0.0, "count": 0})
    entry["value"] += float(amount)
    entry["count"] += count


def get_active_bgs_query(sap_system_id=None):
    """Query Bank Guarantees that are Approved and/or Live in the system."""
    query = BankGuarantee.query.filter(
        db.or_(
            BankGuarantee.status == BGStatus.live,
            BankGuarantee.status == BGStatus.live.value,
            BankGuarantee.status == "live",
            BankGuarantee.status == "approved",
            BankGuarantee.status == "Live",
            BankGuarantee.status == "Approved",
            BankGuarantee.current_stage == "live",
            BankGuarantee.current_stage == "approved",
            BankGuarantee.current_stage == "Live",
            BankGuarantee.current_stage == "Approved",
        )
    )
    if sap_system_id is not None:
        query = query.filter(BankGuarantee.sap_system_id == sap_system_id)
    return query


def _compute_for(sap_system_id, name):
    agg = _empty_aggregate()
    agg["sap_system_id"] = sap_system_id
    agg["sap_system_name"] = name

    active_bgs = get_active_bgs_query(sap_system_id).all()
    agg["active_count"] = len(active_bgs)
    agg["has_data"] = bool(active_bgs)

    confirmed_ids = {
        v.bank_guarantee_id
        for v in BankVerification.query.filter(BankVerification.status == "confirmed").all()
    }

    for bg in active_bgs:
        amount = float(bg.amount or 0)
        agg["total_active_value"] += amount
        if bg.id in confirmed_ids:
            agg["bank_confirmed_value"] += amount
        _add_to(agg["by_bank"], bg.issuing_bank or "Unknown", amount)
        _add_to(agg["by_vendor"], bg.vendor_name or "Unknown", amount)
        bg_t = bg.bg_type.value if hasattr(bg.bg_type, "value") else str(bg.bg_type or "unknown")
        _add_to(agg["by_bg_type"], bg_t.upper(), amount)

    # Business-unit (region/location) grouping from the SAP systems table.
    bu_query = SapSystem.query
    if sap_system_id is not None:
        bu_query = bu_query.filter(SapSystem.id == sap_system_id)
    for sys in bu_query.all():
        subs = get_active_bgs_query(sys.id).all()
        acc = agg["by_business_unit"].setdefault(
            sys.business_unit or sys.display_name, {"value": 0.0, "count": 0}
        )
        for bg in subs:
            acc["value"] += float(bg.amount or 0)
            acc["count"] += 1

    logger.debug("Computed aggregates for sap_system_id=%s (%s): %s active BGs, total value=%.2f",
                 sap_system_id, name, agg["active_count"], agg["total_active_value"])
    return agg


def compute_all():
    """Compute aggregates for every active SAP system plus the company-wide total."""
    result = {"company": _compute_for(None, "Company-wide"), "by_sap": {}}
    for sys in SapSystem.query.all():
        result["by_sap"][sys.id] = _compute_for(sys.id, sys.display_name)
    return result


def _cache_key():
    from flask import current_app

    return current_app.config.get("DASHBOARD_CACHE_KEY", "dashboard_aggregates")


def warm_dashboard_cache():
    aggregates = compute_all()
    try:
        cache.set(_cache_key(), aggregates, timeout=3600)
    except Exception as e:
        logger.warning("Failed to store dashboard aggregates in cache: %s", e)
    return aggregates


def get_aggregates(force_refresh=False):
    key = _cache_key()
    if not force_refresh:
        try:
            cached = cache.get(key)
            if cached is not None:
                return cached
        except Exception:
            pass
    return warm_dashboard_cache()


def get_scoped_aggregates(user, force_refresh=False):
    """Return the cache entry scoped to the user (admin/executives see company-wide)."""
    aggregates = get_aggregates(force_refresh=force_refresh)
    if not aggregates:
        aggregates = warm_dashboard_cache()
    if not aggregates:
        return _empty_aggregate()

    user_roles = set(user.granted_roles or []) if user else set()
    is_admin_or_global = (
        "admin" in user_roles
        or (getattr(user, "active_role", None) in ("admin", "ceo_cfo", "abex"))
        or not getattr(user, "sap_system_id", None)
    )

    if is_admin_or_global:
        return aggregates.get("company") or _empty_aggregate()

    scoped = aggregates.get("by_sap", {}).get(user.sap_system_id)
    if scoped is None:
        sys = SapSystem.query.get(user.sap_system_id)
        if sys:
            scoped = _compute_for(sys.id, sys.display_name)
            aggregates.setdefault("by_sap", {})[user.sap_system_id] = scoped
            try:
                cache.set(_cache_key(), aggregates, timeout=3600)
            except Exception:
                pass
    return scoped or aggregates.get("company") or _empty_aggregate()
