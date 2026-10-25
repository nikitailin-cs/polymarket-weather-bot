"""
Binary decision for a daily-extreme market from the station's running extreme.

Unlike the instantaneous-METAR heuristic, this respects the monotonic nature of
the extreme: a daily *max* can only rise during the day, a daily *min* can only
fall. So a threshold crossing is terminal in one direction and nothing can be
concluded in the other until the day is over.

``yes_if_above`` follows the mapping convention: YES when ``value >= threshold``;
otherwise YES when ``value < threshold``.

States: ``met`` (p=1.0), ``dead`` (p=0.0), ``open`` (p=0.5).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

RESOLUTION_MODEL_DAILY_EXTREMA = "metar_ws_daily_extrema"
NEAR_DEADLINE_MIN = 120


@dataclass(frozen=True)
class ExtremaDecision:
    p_win: float
    binary_state: str  # met | dead | open
    recommended_side: str  # YES | NO | NONE
    reason: str


def minutes_to_deadline(local_now: datetime, deadline_hhmm: str) -> Optional[int]:
    try:
        hh, mm = (int(x) for x in str(deadline_hhmm).strip().split(":")[:2])
    except (TypeError, ValueError):
        return None
    return (hh - local_now.hour) * 60 + (mm - local_now.minute)


def decide_extrema(
    *,
    variable: str,
    yes_if_above: bool,
    threshold: float,
    extreme_value: float,
    local_now: datetime,
    deadline_hhmm: str = "23:59",
    dead_min_gap: float = 15.0,
) -> ExtremaDecision:
    """
    ``variable`` is ``temp_max`` (extreme rises) or ``temp_min`` (extreme falls);
    ``extreme_value`` and ``threshold`` share the contract unit.
    """
    v = (variable or "temp_max").strip().lower()
    rises = v != "temp_min"
    mtd = minutes_to_deadline(local_now, deadline_hhmm)
    after_deadline = mtd is not None and mtd <= 0
    near_deadline = mtd is not None and mtd < NEAR_DEADLINE_MIN

    yes_now = extreme_value >= threshold if yes_if_above else extreme_value < threshold
    # Is the YES condition locked in once true (terminal-true) or locked out once false?
    #   max rises: "value >= thr" is terminal-true, "value < thr" is terminal-false
    #   min falls: "value < thr"  is terminal-true, "value >= thr" is terminal-false
    yes_is_terminal_true = (rises and yes_if_above) or (not rises and not yes_if_above)

    def _d(p: float, state: str, reason: str) -> ExtremaDecision:
        side = "YES" if p > 0.5 else "NO" if p < 0.5 else "NONE"
        return ExtremaDecision(p, state, side, reason)

    if yes_is_terminal_true:
        if yes_now:
            return _d(1.0, "met", "extreme already crossed the threshold")
        if after_deadline:
            return _d(0.0, "dead", "deadline passed without crossing")
        gap = (threshold - extreme_value) if yes_if_above else (extreme_value - threshold)
        if near_deadline and gap > dead_min_gap:
            return _d(0.0, "dead", f"{gap:.1f} short of threshold with <{NEAR_DEADLINE_MIN} min left")
        return _d(0.5, "open", "threshold not reached yet")

    # YES is terminal-false: the extreme crossing the wrong way kills it for good
    if not yes_now:
        return _d(0.0, "dead", "extreme crossed the threshold the wrong way")
    if after_deadline:
        return _d(1.0, "met", "day over, threshold never crossed")
    return _d(0.5, "open", "still alive, settles at end of day")
