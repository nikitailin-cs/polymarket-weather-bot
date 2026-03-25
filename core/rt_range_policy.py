"""
Politique de scalping sur **plages de dates** (``target_date_start`` … ``target_date_end``).

Sans champ ``range_scalp_mode``, le comportement reste : tout jour **dans** la plage
(où ``classify_rt_market_day`` renvoie ``jour_j``) peut produire ``met``/``dead``.
"""
from __future__ import annotations

from datetime import date
from typing import Optional, Tuple


def _parse_iso(d: str) -> Optional[date]:
    s = (d or "").strip()
    if len(s) < 10:
        return None
    try:
        y, m, dd = int(s[0:4]), int(s[5:7]), int(s[8:10])
        return date(y, m, dd)
    except (TypeError, ValueError):
        return None


def narrow_range_scalp_day(
    local_d: date,
    *,
    has_range: bool,
    target_date_start: str,
    target_date_end: str,
    range_scalp_mode: str,
) -> Tuple[str, Optional[str]]:
    """
    Si ``has_range`` et mode restreint : peut transformer un ``jour_j`` logique en
    **prématuré** pour le scalping.

    Retourne ``("jour_j", None)`` ou ``("premature", "range_mode_last_day_only")`` etc.
    """
    if not has_range:
        return "jour_j", None
    mode = (range_scalp_mode or "every_day_in_range").strip().lower()
    if mode in ("", "every_day", "every_day_in_range", "all"):
        return "jour_j", None

    ds = _parse_iso(target_date_start)
    de = _parse_iso(target_date_end)
    if ds is None or de is None:
        return "jour_j", None

    if mode in ("last_day", "last_day_only", "end"):
        if local_d != de:
            return "premature", "range_mode_last_day_only"
        return "jour_j", None

    if mode in ("first_day", "first_day_only", "start"):
        if local_d != ds:
            return "premature", "range_mode_first_day_only"
        return "jour_j", None

    return "jour_j", None
