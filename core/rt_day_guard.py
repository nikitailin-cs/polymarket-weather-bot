"""
Garde jour calendaire pour les contrats RT (bot2 METAR).

- **Jour unique** : ``target_date`` (YYYY-MM-DD) vs date locale lieu.
- **Plage** : ``target_date_start`` … ``target_date_end`` inclus (même format).
"""
from __future__ import annotations

from datetime import date
from typing import Literal

RtDayKind = Literal["invalid", "premature", "past_target", "jour_j"]


def _parse_yyyy_mm_dd(raw: str) -> date | None:
    s = (raw or "").strip()
    if len(s) < 10:
        return None
    try:
        y, m, d = int(s[0:4]), int(s[5:7]), int(s[8:10])
        return date(y, m, d)
    except (TypeError, ValueError):
        return None


def classify_rt_market_day(
    local_d: date,
    target_date_raw: str,
    *,
    target_date_start: str = "",
    target_date_end: str = "",
) -> RtDayKind:
    """
    Compare la date locale au contrat.

    Si ``target_date_start`` et ``target_date_end`` sont tous deux valides,
    la plage prime sur ``target_date_raw`` (mode multi-jours).

    Sinon : même logique qu'avant sur ``target_date_raw`` seul.
    """
    ds = _parse_yyyy_mm_dd(target_date_start)
    de = _parse_yyyy_mm_dd(target_date_end)
    if ds is not None and de is not None:
        if ds > de:
            return "invalid"
        if local_d < ds:
            return "premature"
        if local_d > de:
            return "past_target"
        return "jour_j"

    td = _parse_yyyy_mm_dd(target_date_raw)
    if td is None:
        return "invalid"
    if local_d < td:
        return "premature"
    if local_d > td:
        return "past_target"
    return "jour_j"
