"""PO/SAP financial cross-check service.

A single primary abstraction: given a list of PO numbers, return PO value,
vendor identity, and (for advance guarantees) the open advance amount for each.

If real SAP endpoint configuration is present in `.env`, the real endpoint is
queried. Otherwise it falls back to the local `sap_po_records` dataset. Callers
depend only on this module's return shape, never on which backend produced it.
"""
import logging

import requests

from bgcc.extensions import db
from bgcc.models.sap_reference import SapPoRecord

logger = logging.getLogger("bgcc.sap")


def _query_local(po_numbers):
    records = SapPoRecord.query.filter(
        SapPoRecord.po_number.in_(po_numbers)
    ).all()
    found = {r.po_number: r for r in records}
    missing = [p for p in po_numbers if p not in found]
    if missing:
        raise ValueError(f"PO number(s) not found in financial records: {', '.join(missing)}")
    result = []
    for po in po_numbers:
        r = found[po]
        result.append({
            "po_number": r.po_number,
            "vendor_name": r.vendor_name,
            "po_value": str(r.po_value),
            "currency": r.currency,
            "open_advance_amount": str(r.open_advance_amount) if r.open_advance_amount is not None else None,
            "is_executed": bool(r.is_executed),
        })
    return result


def _query_real_sap(endpoint, po_numbers):
    from flask import current_app

    token = _get_sap_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = {"po_numbers": po_numbers}
    response = requests.post(endpoint, json=payload, headers=headers, timeout=15)
    response.raise_for_status()
    data = response.json()
    items = data.get("items", [])
    found = {item.get("po_number"): item for item in items}
    missing = [p for p in po_numbers if p not in found]
    if missing:
        raise ValueError(f"PO number(s) not found in financial records: {', '.join(missing)}")
    result = []
    for po in po_numbers:
        item = found[po]
        result.append({
            "po_number": item.get("po_number"),
            "vendor_name": item.get("vendor_name"),
            "po_value": item.get("po_value"),
            "currency": item.get("currency", "INR"),
            "open_advance_amount": item.get("open_advance_amount"),
            "is_executed": bool(item.get("is_executed", False)),
        })
    return result


def po_fully_executed(bg):
    """Return True if every PO behind a BG is confirmed fully executed/closed.

    Raises ValueError if the underlying SAP call fails, so the caller can
    surface a retryable error rather than silently defaulting to either
    eligibility outcome.
    """
    po_numbers = bg.po_numbers or []
    if not po_numbers:
        return False
    ctx = get_po_context(po_numbers)
    return all(c.get("is_executed") for c in ctx)


def get_po_context(po_numbers):
    """Return a list of PO context dicts.

    Each dict: {po_number, vendor_name, po_value (str), currency,
                open_advance_amount (str|None), is_executed (bool)}. Raises
                ValueError listing any PO numbers not found so the caller can
                reject the submission.
    """
    from flask import current_app

    po_numbers = [p.strip() for p in po_numbers if p and p.strip()]
    if not po_numbers:
        return []

    endpoint = current_app.config.get("SAP_PO_ENDPOINT")
    if endpoint:
        return _query_real_sap(endpoint, po_numbers)
    return _query_local(po_numbers)


def _get_sap_token():
    from flask import current_app

    base = current_app.config.get("SAP_BASE_URL")
    client_id = current_app.config.get("SAP_CLIENT_ID")
    client_secret = current_app.config.get("SAP_CLIENT_SECRET")
    if not (base and client_id and client_secret):
        return None
    response = requests.post(
        f"{base.rstrip('/')}/oauth/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("access_token")
