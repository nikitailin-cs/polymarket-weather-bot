"""
Shared METAR observation model used by every real-time observation source
(AWC polling registries and the METAR.ws push client).

Kept dependency-free so it can be imported from the asyncio daemon, the
one-shot signal producers and the tests alike.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

_SPECI_HEAD = re.compile(r"^\s*SPECI\b", re.IGNORECASE)


def is_speci_raw(raw_ob: str) -> bool:
    """True when the raw report is a SPECI (special, off-schedule report)."""
    s = (raw_ob or "").strip()
    if not s:
        return False
    return bool(_SPECI_HEAD.match(s))


def parse_utc(value: Any) -> Optional[datetime]:
    """
    Parse an RFC 3339 / ISO 8601 string or a unix timestamp into an aware UTC datetime.
    Returns ``None`` when the value cannot be interpreted.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_utc(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def celsius_to_fahrenheit(temp_c: float) -> float:
    return temp_c * 9.0 / 5.0 + 32.0


def icao_from_raw(raw_ob: str) -> str:
    """
    Extract the station identifier from raw METAR text:
    ``METAR KNYC 251651Z ...`` → ``KNYC``; ``KNYC 251651Z ...`` → ``KNYC``.
    """
    tokens = (raw_ob or "").split()
    for tok in tokens[:3]:
        t = tok.strip().upper()
        if t in ("METAR", "SPECI", "COR", "AUTO"):
            continue
        if re.fullmatch(r"[A-Z0-9]{4}", t):
            return t
        break
    return ""


@dataclass(frozen=True)
class MetarObservation:
    """One decoded surface observation for a station (temperature-centric)."""

    icao_id: str
    temp_celsius: float
    report_time_utc: str
    raw_text: str
    observation_utc: datetime
    is_speci: bool
    dewpoint_celsius: Optional[float] = None
    received_at_utc: Optional[datetime] = None
    source: str = ""

    @property
    def temp_fahrenheit(self) -> float:
        return celsius_to_fahrenheit(self.temp_celsius)

    def dedupe_key(self) -> tuple[str, str, str]:
        """Idempotency key: station + observation time + raw text."""
        return (self.icao_id, format_utc(self.observation_utc), (self.raw_text or "").strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "icao": self.icao_id,
            "temp_c": self.temp_celsius,
            "dewpoint_c": self.dewpoint_celsius,
            "observation_time_utc": format_utc(self.observation_utc),
            "report_time": self.report_time_utc,
            "raw": self.raw_text,
            "is_speci": self.is_speci,
            "received_at_utc": format_utc(self.received_at_utc),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Optional["MetarObservation"]:
        try:
            icao = str(d.get("icao") or d.get("icao_id") or "").strip().upper()
            temp = d.get("temp_c")
            if not icao or temp is None:
                return None
            obs_utc = parse_utc(d.get("observation_time_utc") or d.get("report_time"))
            if obs_utc is None:
                return None
            dew = d.get("dewpoint_c")
            return cls(
                icao_id=icao,
                temp_celsius=float(temp),
                report_time_utc=str(d.get("report_time") or format_utc(obs_utc)),
                raw_text=str(d.get("raw") or ""),
                observation_utc=obs_utc,
                is_speci=bool(d.get("is_speci", is_speci_raw(str(d.get("raw") or "")))),
                dewpoint_celsius=float(dew) if dew is not None else None,
                received_at_utc=parse_utc(d.get("received_at_utc")),
                source=str(d.get("source") or ""),
            )
        except (TypeError, ValueError):
            return None

    def to_bot2_dict(self) -> dict[str, Any]:
        """Shape returned by the historical bot2 ``fetch_latest_metar`` (AWC polling)."""
        return {
            "icao": self.icao_id,
            "temp_c": round(self.temp_celsius, 1),
            "temp_f": round(self.temp_fahrenheit, 1),
            "obsTime": format_utc(self.observation_utc),
            "receiptTime": format_utc(self.received_at_utc),
            "rawOb": self.raw_text,
            "source": self.source,
        }
