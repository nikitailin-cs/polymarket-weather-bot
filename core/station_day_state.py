"""
Per-station daily temperature extrema, computed over the station's *local* day.

Weather markets ("Will the high in NYC on Oct 25 be >= 70F?") settle on the day's
maximum (or minimum), not on the latest reading. A polling bot only sees the
instantaneous METAR temperature; with METAR.ws push delivery every routine METAR
*and* every SPECI arrives, so the day's running extremes can be maintained exactly:

- the local calendar day comes from the station IANA timezone (mapping ``timezone``);
- the body temperature is whole degrees C; the ``RMK T`` group gives tenths and is
  preferred when present (``T01720061`` -> 17.2 / 6.1);
- SPECI reports count like any observation (they often carry the actual peak);
- ``RMK 1sTTT`` / ``2sTTT`` six-hour max/min groups are applied when their whole
  6-hour window lies inside the same local day (they can reveal a peak between
  routine reports); ``4sTTTsTTT`` 24-hour groups are recorded for audit only.

:class:`DayStateTracker` keeps a few days per station and (de)serialises to the
daemon snapshot so a restart does not forget the morning's readings.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from core.metar_observation import MetarObservation, format_utc, parse_utc

LOG = logging.getLogger("station_day_state")

MAX_SAMPLES = 400

_T_GROUP = re.compile(r"(?<![A-Z0-9])T([01])(\d{3})([01])(\d{3})(?![A-Z0-9])")
_SIX_MAX = re.compile(r"(?<![A-Z0-9/])1([01])(\d{3})(?![A-Z0-9])")
_SIX_MIN = re.compile(r"(?<![A-Z0-9/])2([01])(\d{3})(?![A-Z0-9])")
_DAY_MAXMIN = re.compile(r"(?<![A-Z0-9/])4([01])(\d{3})([01])(\d{3})(?![A-Z0-9])")


def _tenths(sign: str, digits: str) -> float:
    v = int(digits) / 10.0
    return -v if sign == "1" else v


@dataclass(frozen=True)
class MetarRemarks:
    temp_tenths_c: Optional[float] = None
    dewpoint_tenths_c: Optional[float] = None
    six_hour_max_c: Optional[float] = None
    six_hour_min_c: Optional[float] = None
    day_max_c: Optional[float] = None
    day_min_c: Optional[float] = None


def parse_remarks(raw: str) -> MetarRemarks:
    """Extract temperature groups from the ``RMK`` section of a raw METAR/SPECI."""
    text = (raw or "").upper()
    idx = text.find(" RMK")
    if idx < 0:
        return MetarRemarks()
    rmk = text[idx + 4 :]
    temp = dew = smax = smin = dmax = dmin = None
    m = _T_GROUP.search(rmk)
    if m:
        temp = _tenths(m.group(1), m.group(2))
        dew = _tenths(m.group(3), m.group(4))
    m = _SIX_MAX.search(rmk)
    if m:
        smax = _tenths(m.group(1), m.group(2))
    m = _SIX_MIN.search(rmk)
    if m:
        smin = _tenths(m.group(1), m.group(2))
    m = _DAY_MAXMIN.search(rmk)
    if m:
        dmax = _tenths(m.group(1), m.group(2))
        dmin = _tenths(m.group(3), m.group(4))
    return MetarRemarks(temp, dew, smax, smin, dmax, dmin)


def precise_temperature_c(obs: MetarObservation, remarks: Optional[MetarRemarks] = None) -> float:
    """Tenths-precision temperature when the T group agrees with the body value."""
    rm = remarks or parse_remarks(obs.raw_text)
    if rm.temp_tenths_c is not None and abs(rm.temp_tenths_c - obs.temp_celsius) < 1.0:
        return rm.temp_tenths_c
    return float(obs.temp_celsius)


def local_date_for(obs_utc: datetime, tz_name: str) -> date:
    try:
        return obs_utc.astimezone(ZoneInfo(tz_name or "UTC")).date()
    except Exception:  # noqa: BLE001 - unknown zone -> UTC
        LOG.warning("unknown timezone %r, using UTC", tz_name)
        return obs_utc.astimezone(timezone.utc).date()


@dataclass
class StationDayState:
    icao: str
    local_date: str  # YYYY-MM-DD
    timezone: str
    max_c: Optional[float] = None
    max_time_utc: str = ""
    max_raw: str = ""
    max_source: str = ""  # metar | speci | rmk_6h
    min_c: Optional[float] = None
    min_time_utc: str = ""
    min_raw: str = ""
    min_source: str = ""
    obs_count: int = 0
    speci_count: int = 0
    first_obs_utc: str = ""
    last_obs_utc: str = ""
    last_temp_c: Optional[float] = None
    reported_24h_max_c: Optional[float] = None
    reported_24h_min_c: Optional[float] = None
    samples: list[list[Any]] = field(default_factory=list)  # [time_utc, temp_c, is_speci]
    _seen: set[tuple[str, str]] = field(default_factory=set, repr=False, compare=False)

    # -- updates -----------------------------------------------------------------

    def _consider_max(self, value: float, time_utc: str, raw: str, source: str) -> bool:
        if self.max_c is None or value > self.max_c:
            self.max_c, self.max_time_utc, self.max_raw, self.max_source = value, time_utc, raw, source
            return True
        return False

    def _consider_min(self, value: float, time_utc: str, raw: str, source: str) -> bool:
        if self.min_c is None or value < self.min_c:
            self.min_c, self.min_time_utc, self.min_raw, self.min_source = value, time_utc, raw, source
            return True
        return False

    def ingest(self, obs: MetarObservation, remarks: Optional[MetarRemarks] = None) -> bool:
        """Apply one observation; returns True when it changed the max or the min."""
        key = (format_utc(obs.observation_utc), "speci" if obs.is_speci else "metar")
        if key in self._seen:
            return False
        self._seen.add(key)
        rm = remarks or parse_remarks(obs.raw_text)
        value = precise_temperature_c(obs, rm)
        t = format_utc(obs.observation_utc)
        src = "speci" if obs.is_speci else "metar"

        changed = self._consider_max(value, t, obs.raw_text, src)
        changed = self._consider_min(value, t, obs.raw_text, src) or changed

        self.obs_count += 1
        if obs.is_speci:
            self.speci_count += 1
        if not self.first_obs_utc or t < self.first_obs_utc:
            self.first_obs_utc = t
        if t >= self.last_obs_utc:
            self.last_obs_utc = t
            self.last_temp_c = value
        self.samples.append([t, value, bool(obs.is_speci)])
        if len(self.samples) > MAX_SAMPLES:
            self.samples = self.samples[-MAX_SAMPLES:]
        self.samples.sort(key=lambda s: s[0])

        # 6-hour groups: only when the whole window sits inside this local day
        if rm.six_hour_max_c is not None or rm.six_hour_min_c is not None:
            start = obs.observation_utc - timedelta(hours=6)
            if local_date_for(start, self.timezone).isoformat() == self.local_date:
                if rm.six_hour_max_c is not None:
                    changed = self._consider_max(rm.six_hour_max_c, t, obs.raw_text, "rmk_6h") or changed
                if rm.six_hour_min_c is not None:
                    changed = self._consider_min(rm.six_hour_min_c, t, obs.raw_text, "rmk_6h") or changed
        if rm.day_max_c is not None:
            self.reported_24h_max_c = rm.day_max_c
        if rm.day_min_c is not None:
            self.reported_24h_min_c = rm.day_min_c
        return changed

    # -- (de)serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "icao": self.icao,
            "local_date": self.local_date,
            "timezone": self.timezone,
            "max_c": self.max_c,
            "max_time_utc": self.max_time_utc,
            "max_raw": self.max_raw,
            "max_source": self.max_source,
            "min_c": self.min_c,
            "min_time_utc": self.min_time_utc,
            "min_raw": self.min_raw,
            "min_source": self.min_source,
            "obs_count": self.obs_count,
            "speci_count": self.speci_count,
            "first_obs_utc": self.first_obs_utc,
            "last_obs_utc": self.last_obs_utc,
            "last_temp_c": self.last_temp_c,
            "reported_24h_max_c": self.reported_24h_max_c,
            "reported_24h_min_c": self.reported_24h_min_c,
            "samples": [list(s) for s in self.samples],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Optional["StationDayState"]:
        try:
            st = cls(
                icao=str(d["icao"]).upper(),
                local_date=str(d["local_date"]),
                timezone=str(d.get("timezone") or "UTC"),
                max_c=_opt_float(d.get("max_c")),
                max_time_utc=str(d.get("max_time_utc") or ""),
                max_raw=str(d.get("max_raw") or ""),
                max_source=str(d.get("max_source") or ""),
                min_c=_opt_float(d.get("min_c")),
                min_time_utc=str(d.get("min_time_utc") or ""),
                min_raw=str(d.get("min_raw") or ""),
                min_source=str(d.get("min_source") or ""),
                obs_count=int(d.get("obs_count") or 0),
                speci_count=int(d.get("speci_count") or 0),
                first_obs_utc=str(d.get("first_obs_utc") or ""),
                last_obs_utc=str(d.get("last_obs_utc") or ""),
                last_temp_c=_opt_float(d.get("last_temp_c")),
                reported_24h_max_c=_opt_float(d.get("reported_24h_max_c")),
                reported_24h_min_c=_opt_float(d.get("reported_24h_min_c")),
                samples=[list(s) for s in d.get("samples") or [] if isinstance(s, (list, tuple)) and len(s) >= 2],
            )
        except (KeyError, TypeError, ValueError):
            return None
        for s in st.samples:
            st._seen.add((str(s[0]), "speci" if len(s) > 2 and s[2] else "metar"))
        return st

    @property
    def max_is_speci(self) -> bool:
        return self.max_source == "speci"

    @property
    def min_is_speci(self) -> bool:
        return self.min_source == "speci"


def _opt_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class DayStateTracker:
    """Running extremes for many stations, keyed by (ICAO, local date)."""

    def __init__(self, tz_resolver: Callable[[str], str], *, keep_days: int = 3) -> None:
        self._tz = tz_resolver
        self.keep_days = max(1, keep_days)
        self._states: dict[tuple[str, str], StationDayState] = {}

    def timezone_for(self, icao: str) -> str:
        try:
            return str(self._tz(icao) or "UTC")
        except Exception:  # noqa: BLE001
            return "UTC"

    def ingest(self, obs: MetarObservation) -> StationDayState:
        tz = self.timezone_for(obs.icao_id)
        day = local_date_for(obs.observation_utc, tz).isoformat()
        key = (obs.icao_id, day)
        st = self._states.get(key)
        if st is None:
            st = StationDayState(icao=obs.icao_id, local_date=day, timezone=tz)
            self._states[key] = st
        st.ingest(obs)
        return st

    def get(self, icao: str, local_day: date | str) -> Optional[StationDayState]:
        d = local_day.isoformat() if isinstance(local_day, date) else str(local_day)
        return self._states.get(((icao or "").upper(), d))

    def days_for(self, icao: str) -> list[StationDayState]:
        code = (icao or "").upper()
        return sorted((s for (i, _), s in self._states.items() if i == code), key=lambda s: s.local_date)

    def prune(self, now_utc: Optional[datetime] = None) -> int:
        """Drop days older than ``keep_days`` (judged in each station's local zone)."""
        now_utc = now_utc or datetime.now(timezone.utc)
        removed = 0
        for key in list(self._states):
            icao, day = key
            today = local_date_for(now_utc, self._states[key].timezone)
            try:
                d = date.fromisoformat(day)
            except ValueError:
                d = today
            if (today - d).days >= self.keep_days:
                del self._states[key]
                removed += 1
        return removed

    def to_dict(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for (icao, day), st in sorted(self._states.items()):
            out.setdefault(icao, {})[day] = st.to_dict()
        return out

    def restore(self, days: Any) -> int:
        """Load states from a snapshot ``days`` mapping; existing states are kept."""
        if not isinstance(days, dict):
            return 0
        n = 0
        for icao, by_day in days.items():
            if not isinstance(by_day, dict):
                continue
            for day, row in by_day.items():
                if not isinstance(row, dict):
                    continue
                st = StationDayState.from_dict(row)
                if st is None:
                    continue
                key = (str(icao).upper(), str(day))
                if key not in self._states:
                    self._states[key] = st
                    n += 1
        return n

    def __len__(self) -> int:
        return len(self._states)


def day_state_from_snapshot(snapshot: dict[str, Any], icao: str, local_day: date | str) -> Optional[StationDayState]:
    """Read one (station, day) state straight from a snapshot payload."""
    days = snapshot.get("days") if isinstance(snapshot, dict) else None
    if not isinstance(days, dict):
        return None
    by_day = days.get((icao or "").upper())
    if not isinstance(by_day, dict):
        return None
    d = local_day.isoformat() if isinstance(local_day, date) else str(local_day)
    row = by_day.get(d)
    return StationDayState.from_dict(row) if isinstance(row, dict) else None


__all__ = [
    "DayStateTracker",
    "MetarRemarks",
    "StationDayState",
    "day_state_from_snapshot",
    "local_date_for",
    "parse_remarks",
    "parse_utc",
    "precise_temperature_c",
]
