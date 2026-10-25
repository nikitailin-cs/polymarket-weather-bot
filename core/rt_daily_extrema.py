"""
Binary decision for a daily-extreme market from the station's running extreme.

Polymarket daily temperature events are *buckets*: "15C or below", "16C", ...,
"25C or higher". A bucket is the inclusive range ``[low, high]`` with ``None``
for an open end. The classic single-threshold mapping (``threshold`` +
``yes_if_above``) is the special case ``[threshold, None]`` / ``[None, threshold)``.

The decision respects the monotonic nature of the extreme: a daily *max* can
only rise during the day, a daily *min* can only fall.

States: ``met`` (p=1.0), ``dead`` (p=0.0), ``open`` (p=0.5).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

RESOLUTION_MODEL_DAILY_EXTREMA = "metar_ws_daily_extrema"
NEAR_DEADLINE_MIN = 120


@dataclass(frozen=True)
class ExtremaDecision:
    p_win: float
    binary_state: str  # met | dead | open
    recommended_side: str  # YES | NO | NONE
    reason: str


@dataclass(frozen=True)
class Bucket:
    low: Optional[float]  # None = no lower bound
    high: Optional[float]  # None = no upper bound

    def contains(self, v: float) -> bool:
        return (self.low is None or v >= self.low) and (self.high is None or v <= self.high)

    @classmethod
    def from_mapping(cls, cfg: dict[str, Any]) -> Optional["Bucket"]:
        """``threshold_low`` / ``threshold_high`` if present, else the legacy threshold form."""
        if "threshold_low" in cfg or "threshold_high" in cfg:
            lo, hi = _opt(cfg.get("threshold_low")), _opt(cfg.get("threshold_high"))
            if lo is None and hi is None:
                return None
            return cls(lo, hi)
        thr = _opt(cfg.get("threshold"))
        if thr is None:
            return None
        if bool(cfg.get("yes_if_above", True)):
            return cls(thr, None)
        # legacy "YES if value < threshold": whole-degree markets -> high = threshold - 1
        step = 1.0 if float(thr).is_integer() else 0.0
        return cls(None, thr - step if step else thr)


def _opt(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def minutes_to_deadline(local_now: datetime, deadline_hhmm: str) -> Optional[int]:
    try:
        hh, mm = (int(x) for x in str(deadline_hhmm).strip().split(":")[:2])
    except (TypeError, ValueError):
        return None
    return (hh - local_now.hour) * 60 + (mm - local_now.minute)


def _d(p: float, state: str, reason: str) -> ExtremaDecision:
    side = "YES" if p > 0.5 else "NO" if p < 0.5 else "NONE"
    return ExtremaDecision(p, state, side, reason)


def decide_bucket(
    *,
    variable: str,
    bucket: Bucket,
    extreme_value: float,
    local_now: datetime,
    deadline_hhmm: str = "23:59",
    dead_min_gap: float = 15.0,
) -> ExtremaDecision:
    """
    ``variable``: ``temp_max`` (extreme rises) or ``temp_min`` (extreme falls).
    ``extreme_value`` and the bucket share the contract unit and precision.
    """
    rises = (variable or "temp_max").strip().lower() != "temp_min"
    mtd = minutes_to_deadline(local_now, deadline_hhmm)
    after = mtd is not None and mtd <= 0
    near = mtd is not None and mtd < NEAR_DEADLINE_MIN
    lo, hi = bucket.low, bucket.high

    if rises:
        if hi is not None and extreme_value > hi:
            return _d(0.0, "dead", f"max {extreme_value:g} already above bucket top {hi:g}")
        if lo is not None and extreme_value < lo:
            if after:
                return _d(0.0, "dead", "deadline passed below the bucket")
            if near and lo - extreme_value > dead_min_gap:
                return _d(0.0, "dead", f"{lo - extreme_value:g} short of bucket with <{NEAR_DEADLINE_MIN} min left")
            return _d(0.5, "open", "bucket not reached yet")
        # inside the bucket
        if hi is None:
            return _d(1.0, "met", "max reached the open-ended bucket")
        if after:
            return _d(1.0, "met", "day over inside the bucket")
        return _d(0.5, "open", "inside the bucket, max can still rise")

    # falling extreme (daily minimum)
    if lo is not None and extreme_value < lo:
        return _d(0.0, "dead", f"min {extreme_value:g} already below bucket bottom {lo:g}")
    if hi is not None and extreme_value > hi:
        if after:
            return _d(0.0, "dead", "deadline passed above the bucket")
        if near and extreme_value - hi > dead_min_gap:
            return _d(0.0, "dead", f"{extreme_value - hi:g} above bucket with <{NEAR_DEADLINE_MIN} min left")
        return _d(0.5, "open", "bucket not reached yet")
    if lo is None:
        return _d(1.0, "met", "min reached the open-ended bucket")
    if after:
        return _d(1.0, "met", "day over inside the bucket")
    return _d(0.5, "open", "inside the bucket, min can still fall")


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
    """Legacy single-threshold form: YES if ``value >= threshold`` else YES if ``value < threshold``."""
    bucket = Bucket.from_mapping({"threshold": threshold, "yes_if_above": yes_if_above})
    assert bucket is not None
    return decide_bucket(
        variable=variable,
        bucket=bucket,
        extreme_value=extreme_value,
        local_now=local_now,
        deadline_hhmm=deadline_hhmm,
        dead_min_gap=dead_min_gap,
    )
