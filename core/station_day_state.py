"""
Per-station daily temperature extrema, computed over the station's *local* day,
following the Polymarket daily temperature resolution rules (events resolve on
the aviationweather.gov METAR feed):

- only **routine** reports count: a report whose line begins with ``SPECI`` is
  excluded, and so are the frequent non-METAR ``obs10`` observations (MADIS/KNMI);
  everything else is routine, even off-hour or unlabeled;
- the qualifying value is the ``RMK T`` group in tenths of a degree Celsius; if
  a routine report has no T group, the whole-degree body temperature governs;
- the value is rounded to the nearest whole degree in the market unit, halves
  rounded up (16.5 -> 17, -8.5 -> -8); Fahrenheit markets convert first
  (``F = C * 9/5 + 32``) then round;
- an observation belongs to the local calendar day of its time stamp (station
  IANA timezone, DST included).

SPECI reports and the 6-hour ``1sTTT``/``2sTTT`` and 24-hour ``4sTTTsTTT``
remark groups are therefore **not** part of the resolution extreme. They are
still tracked as a *leading* extreme (``lead_max_c`` / ``lead_min_c``) because a
SPECI peak usually shows up in the next routine report.

:class:`DayStateTracker` keeps a few days per station and (de)serialises to the
daemon snapshot so a restart does not forget the morning's readings.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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


def qualifying_temperature_c(obs: MetarObservation, remarks: Optional[MetarRemarks] = None) -> tuple[float, str]:
    """
    The value the resolution rule reads from one report: T-group tenths when present
    (and consistent with the body), else the whole-degree body temperature.
    Returns ``(value_c, source)`` with source ``"t_group"`` or ``"body"``.
    """
    rm = remarks or parse_remarks(obs.raw_text)
    if rm.temp_tenths_c is not None and abs(rm.temp_tenths_c - obs.temp_celsius) < 1.0:
        return rm.temp_tenths_c, "t_group"
    return float(obs.temp_celsius), "body"


def precise_temperature_c(obs: MetarObservation, remarks: Optional[MetarRemarks] = None) -> float:
    return qualifying_temperature_c(obs, remarks)[0]


def resolution_value(value_c: Optional[float], unit: str) -> Optional[int]:
    """
    Whole-degree value in the market unit, halves rounded up: Celsius markets round
    the tenths; Fahrenheit markets convert with ``C * 9/5 + 32`` first.
    16.5 -> 17, 17.4 -> 17, -1.7 -> -2, -8.5 -> -8; 18.3C -> 64.94F -> 65.
    """
    if value_c is None:
        return None
    x = float(value_c)
    if not (unit or "F").upper().startswith("C"):
        x = x * 9.0 / 5.0 + 32.0
    return int(math.floor(x + 0.5))


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
    # resolution extremes: routine reports only, qualifying value (tenths or body)
    max_c: Optional[float] = None
    max_time_utc: str = ""
    max_raw: str = ""
    max_source: str = ""  # t_group | body
    min_c: Optional[float] = None
    min_time_utc: str = ""
    min_raw: str = ""
    min_source: str = ""
    # leading extremes: every report incl. SPECI (not used for resolution)
    lead_max_c: Optional[float] = None
    lead_max_time_utc: str = ""
    lead_max_is_speci: bool = False
    lead_min_c: Optional[float] = None
    lead_min_time_utc: str = ""
    lead_min_is_speci: bool = False
    obs_count: int = 0  # routine reports
    speci_count: int = 0
    fast_count: int = 0  # obs10 frequent observations (leading only)
    first_obs_utc: str = ""
    last_obs_utc: str = ""
    last_temp_c: Optional[float] = None
    reported_6h_max_c: Optional[float] = None
    reported_6h_min_c: Optional[float] = None
    reported_24h_max_c: Optional[float] = None
    reported_24h_min_c: Optional[float] = None
    samples: list[list[Any]] = field(default_factory=list)  # [time_utc, temp_c, is_speci]
    _seen: set[tuple[str, str]] = field(default_factory=set, repr=False, compare=False)

    # -- updates -----------------------------------------------------------------

    def ingest(self, obs: MetarObservation, remarks: Optional[MetarRemarks] = None) -> bool:
        """Apply one observation; returns True when it changed the resolution max or min."""
        kind = obs.report_kind
        key = (format_utc(obs.observation_utc), kind)
        if key in self._seen:
            return False
        self._seen.add(key)
        rm = remarks or parse_remarks(obs.raw_text)
        value, src = qualifying_temperature_c(obs, rm)
        t = format_utc(obs.observation_utc)
        changed = False

        if self.lead_max_c is None or value > self.lead_max_c:
            self.lead_max_c, self.lead_max_time_utc, self.lead_max_is_speci = value, t, obs.is_speci
        if self.lead_min_c is None or value < self.lead_min_c:
            self.lead_min_c, self.lead_min_time_utc, self.lead_min_is_speci = value, t, obs.is_speci

        if kind == "speci":
            self.speci_count += 1
        elif kind == "obs10":
            self.fast_count += 1
        else:
            if self.max_c is None or value > self.max_c:
                self.max_c, self.max_time_utc, self.max_raw, self.max_source = value, t, obs.raw_text, src
                changed = True
            if self.min_c is None or value < self.min_c:
                self.min_c, self.min_time_utc, self.min_raw, self.min_source = value, t, obs.raw_text, src
                changed = True
            self.obs_count += 1

        if not self.first_obs_utc or t < self.first_obs_utc:
            self.first_obs_utc = t
        if t >= self.last_obs_utc:
            self.last_obs_utc = t
            self.last_temp_c = value
        self.samples.append([t, value, kind != "metar", kind])
        if len(self.samples) > MAX_SAMPLES:
            self.samples = self.samples[-MAX_SAMPLES:]
        self.samples.sort(key=lambda s: s[0])

        if rm.six_hour_max_c is not None:
            self.reported_6h_max_c = rm.six_hour_max_c
        if rm.six_hour_min_c is not None:
            self.reported_6h_min_c = rm.six_hour_min_c
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
            "lead_max_c": self.lead_max_c,
            "lead_max_time_utc": self.lead_max_time_utc,
            "lead_max_is_speci": self.lead_max_is_speci,
            "lead_min_c": self.lead_min_c,
            "lead_min_time_utc": self.lead_min_time_utc,
            "lead_min_is_speci": self.lead_min_is_speci,
            "obs_count": self.obs_count,
            "speci_count": self.speci_count,
            "fast_count": self.fast_count,
            "first_obs_utc": self.first_obs_utc,
            "last_obs_utc": self.last_obs_utc,
            "last_temp_c": self.last_temp_c,
            "reported_6h_max_c": self.reported_6h_max_c,
            "reported_6h_min_c": self.reported_6h_min_c,
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
                lead_max_c=_opt_float(d.get("lead_max_c")),
                lead_max_time_utc=str(d.get("lead_max_time_utc") or ""),
                lead_max_is_speci=bool(d.get("lead_max_is_speci", False)),
                lead_min_c=_opt_float(d.get("lead_min_c")),
                lead_min_time_utc=str(d.get("lead_min_time_utc") or ""),
                lead_min_is_speci=bool(d.get("lead_min_is_speci", False)),
                obs_count=int(d.get("obs_count") or 0),
                speci_count=int(d.get("speci_count") or 0),
                fast_count=int(d.get("fast_count") or 0),
                first_obs_utc=str(d.get("first_obs_utc") or ""),
                last_obs_utc=str(d.get("last_obs_utc") or ""),
                last_temp_c=_opt_float(d.get("last_temp_c")),
                reported_6h_max_c=_opt_float(d.get("reported_6h_max_c")),
                reported_6h_min_c=_opt_float(d.get("reported_6h_min_c")),
                reported_24h_max_c=_opt_float(d.get("reported_24h_max_c")),
                reported_24h_min_c=_opt_float(d.get("reported_24h_min_c")),
                samples=[list(s) for s in d.get("samples") or [] if isinstance(s, (list, tuple)) and len(s) >= 2],
            )
        except (KeyError, TypeError, ValueError):
            return None
        for s in st.samples:
            kind = str(s[3]) if len(s) > 3 else ("speci" if len(s) > 2 and s[2] else "metar")
            st._seen.add((str(s[0]), kind))
        return st

    @property
    def max_is_speci(self) -> bool:  # resolution extremes never come from SPECI
        return False

    @property
    def min_is_speci(self) -> bool:
        return False


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
                if st.timezone != self.timezone_for(st.icao):
                    LOG.info("dropping restored %s %s: timezone %s is now %s", st.icao, day, st.timezone, self.timezone_for(st.icao))
                    continue
                if "lead_max_c" not in row:
                    LOG.info("dropping restored %s %s: built by an older schema", st.icao, day)
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
    "qualifying_temperature_c",
    "resolution_value",
]
