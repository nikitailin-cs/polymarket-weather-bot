"""
Extraction ``YYYY-MM-DD`` (jour d’événement / résolution) pour le registre bot1.

Priorité : date explicite dans la question → ``end_date_iso`` Gamma → date UTC du marché.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_RE_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_RE_MONTH_DAY_YEAR = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(20\d{2})\b",
    re.I,
)


def infer_target_date_yyyy_mm_dd(
    *,
    question: str,
    end_date_iso: str,
    market_end_utc: Optional[datetime],
) -> Optional[str]:
    """
    Déduit ``target_date`` pour ``active_markets.json`` / GFS jour J.

    Heuristique : titre Polymarket > champ Gamma > fin de marché UTC.
    """
    q = question or ""

    m = _RE_ISO.search(q)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mo, d).date().isoformat()
        except ValueError:
            pass

    m2 = _RE_MONTH_DAY_YEAR.search(q.lower())
    if m2:
        mon = _MONTHS.get(m2.group(1).lower())
        if mon:
            try:
                day = int(m2.group(2))
                year = int(m2.group(3))
                return datetime(year, mon, day).date().isoformat()
            except ValueError:
                pass

    es = (end_date_iso or "").strip()
    if len(es) >= 10 and es[4] == "-" and es[7] == "-":
        try:
            datetime.strptime(es[:10], "%Y-%m-%d")
            return es[:10]
        except ValueError:
            pass

    if market_end_utc is not None:
        dt = market_end_utc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat()

    return None
