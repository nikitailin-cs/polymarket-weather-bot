"""
Agrégats de santé pour un cycle ``realtime_signals.json`` (bot2).
"""
from __future__ import annotations

from typing import Any


def summarize_signals_health(signals: dict[str, Any]) -> dict[str, Any]:
    """
    Compte les états ``binary_state`` et propose un ``health_status`` :
    ``ok`` | ``degraded`` | ``error``.
    """
    counts: dict[str, int] = {}
    n = 0
    for mid, row in signals.items():
        if not isinstance(mid, str) or mid.startswith("_"):
            continue
        if not isinstance(row, dict):
            continue
        n += 1
        bs = str(row.get("binary_state", "") or "unknown").strip().lower()
        counts[bs] = counts.get(bs, 0) + 1

    met = counts.get("met", 0)
    dead = counts.get("dead", 0)
    unsupported = counts.get("unsupported_variable", 0)
    metar_fail = counts.get("metar_unavailable", 0)
    premature = counts.get("premature", 0)

    if n == 0:
        health = "error"
    elif metar_fail > 0 and met + dead == 0:
        health = "degraded"
    elif unsupported > 0:
        health = "degraded"
    else:
        health = "ok"

    return {
        "markets_total": n,
        "counts_by_binary_state": counts,
        "health_status": health,
        "scalp_ready_met_or_dead": met + dead,
    }
