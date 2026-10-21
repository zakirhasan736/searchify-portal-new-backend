"""Shared live / measured / unavailable semantics for Searchify feature payloads.

A zero is valid only when a provider measured zero. Unknown and failed states must
not be silently coerced into successful empty or zero-filled reports.
"""

from __future__ import annotations

LIVE = "google-live-v1"
ESTIMATE = "operator-estimate-v1"
OPERATOR = "searchify_operator"


def is_live_payload(payload: dict | None) -> bool:
    """True when the row is Google-synced or an explicit operator estimate (not catalog demo)."""
    p = payload or {}
    seed = str(p.get("seedVersion") or "")
    source = str(p.get("source") or "")
    if seed == LIVE or source.startswith("google"):
        return True
    if source == OPERATOR or seed == ESTIMATE:
        return True
    return False


def is_measured_payload(payload: dict | None) -> bool:
    """True when the payload claims a real provider/Google measurement (not an estimate)."""
    p = payload or {}
    if p.get("estimate") is True or p.get("inferred") is True:
        return False
    if p.get("measured") is False and p.get("observed") is False:
        return False
    if p.get("live") is False and p.get("estimate") is True:
        return False
    seed = str(p.get("seedVersion") or "")
    source = str(p.get("source") or "")
    return seed == LIVE or source.startswith("google")


def unavailable(*, reason: str, data_kind: str, provider: str = "") -> dict:
    return {
        "status": "unavailable",
        "summary": reason,
        "rows": [],
        "measured": False,
        "observed": False,
        "live": False,
        "estimate": False,
        "inferred": False,
        "dataKind": data_kind,
        "provider": provider or "",
        "disclaimer": reason,
    }
