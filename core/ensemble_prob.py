"""
Helpers de probabilités empiriques sur ensembles (GEFS, etc.).
"""
from __future__ import annotations

from typing import Iterable, Sequence


def empirical_prob_above(values: Sequence[float], threshold: float, *, strict: bool = False) -> float:
    if not values:
        return 0.0
    if strict:
        wins = sum(1 for v in values if v > threshold)
    else:
        wins = sum(1 for v in values if v >= threshold)
    return wins / float(len(values))


def empirical_prob_below(values: Sequence[float], threshold: float, *, strict: bool = False) -> float:
    if not values:
        return 0.0
    if strict:
        wins = sum(1 for v in values if v < threshold)
    else:
        wins = sum(1 for v in values if v <= threshold)
    return wins / float(len(values))


def empirical_prob_from_operator(values: Sequence[float], operator: str, threshold: float) -> float:
    op = (operator or "").strip().lower()
    if op in ("gte", ">="):
        return empirical_prob_above(values, threshold, strict=False)
    if op in ("gt", ">"):
        return empirical_prob_above(values, threshold, strict=True)
    if op in ("lte", "<="):
        return empirical_prob_below(values, threshold, strict=False)
    if op in ("lt", "<"):
        return empirical_prob_below(values, threshold, strict=True)
    # eq : proxy via voisinage ±0.5 (même convention que parse « exact » historique)
    if op in ("eq", "==", "exact"):
        if not values:
            return 0.0
        wins = sum(1 for v in values if abs(v - threshold) <= 0.5)
        return wins / float(len(values))
    return 0.0


def quantile(values: Sequence[float], q: float) -> float:
    if not values:
        raise ValueError("values vide")
    if q <= 0:
        return float(min(values))
    if q >= 1:
        return float(max(values))
    s = sorted(float(v) for v in values)
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def blend_prob(p_primary: float, p_secondary: float, *, alpha: float) -> float:
    a = max(0.0, min(1.0, float(alpha)))
    p = a * float(p_primary) + (1.0 - a) * float(p_secondary)
    return max(0.0, min(1.0, p))

