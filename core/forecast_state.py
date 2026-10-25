"""
Model forecasts from the METAR.ws stream (``metar.forecast.basic.<icao>`` /
``metar.forecast.hires.<icao>``) and the probability they imply for a bucket.

One publication = one (station, model, run, local day) record::

    {"station": "KLGA", "model": "gfs", "tier": "basic", "source": "noaa",
     "run_utc": "2026-10-25T06:00:00Z", "local_date": "2026-10-27",
     "temp_max_c": "15.9", "temp_min_c": "11.0",
     "max_valid_utc": "2026-10-27T20:00:00Z", "min_valid_utc": "2026-10-27T12:00:00Z",
     "local_day_complete": true, "status": "day_complete",
     "change": {"previous_run_utc": ..., "temp_max_delta_c": "-0.1", ...}}

:class:`ForecastTracker` keeps, per (station, local day), the latest run of every
model. :func:`bucket_probability` turns those forecasts plus the observed
routine extreme so far into ``P(resolution value falls in the bucket)`` with a
simple, uncalibrated normal mixture:

- each model run that covers the whole local day contributes a normal around its
  forecast extreme with a sigma per tier (``FORECAST_SIGMA_HIRES_C`` 1.3,
  ``FORECAST_SIGMA_BASIC_C`` 1.9 by default); a run covering only part of the day
  is used as a bound (the day's max is at least its max), never as a centre;
- the day's max cannot end below the routine max already observed (and the min
  cannot end above the observed min): the mixture is conditioned on that;
- once the forecast peak time is well past (``FORECAST_PEAK_PASSED_HOURS`` 2),
  the observed extreme dominates (sigma ``FORECAST_SIGMA_LATE_C`` 0.6).

The bucket is expressed in the market unit on whole degrees with halves up, so
it is mapped back to the Celsius interval that rounds into it before integrating.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from core.metar_observation import format_utc, parse_utc

LOG = logging.getLogger("forecast_state")


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class ForecastRecord:
    station: str
    model: str
    tier: str  # basic | hires
    source: str
    run_utc: datetime
    local_date: str
    temp_max_c: Optional[float]
    temp_min_c: Optional[float]
    max_valid_utc: Optional[datetime]
    min_valid_utc: Optional[datetime]
    complete: bool
    status: str
    receipt_utc: Optional[datetime]
    hours_available: Optional[int] = None
    max_delta_c: Optional[float] = None
    min_delta_c: Optional[float] = None
    previous_run_utc: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "model": self.model,
            "tier": self.tier,
            "source": self.source,
            "run_utc": format_utc(self.run_utc),
            "local_date": self.local_date,
            "temp_max_c": self.temp_max_c,
            "temp_min_c": self.temp_min_c,
            "max_valid_utc": format_utc(self.max_valid_utc),
            "min_valid_utc": format_utc(self.min_valid_utc),
            "complete": self.complete,
            "status": self.status,
            "receipt_utc": format_utc(self.receipt_utc),
            "hours_available": self.hours_available,
            "max_delta_c": self.max_delta_c,
            "min_delta_c": self.min_delta_c,
            "previous_run_utc": format_utc(self.previous_run_utc),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Optional["ForecastRecord"]:
        run = parse_utc(d.get("run_utc"))
        station = str(d.get("station") or "").upper()
        model = str(d.get("model") or "").lower()
        day = str(d.get("local_date") or "")
        if not (run and station and model and day):
            return None
        return cls(
            station=station,
            model=model,
            tier=str(d.get("tier") or "").lower(),
            source=str(d.get("source") or ""),
            run_utc=run,
            local_date=day,
            temp_max_c=_opt_float(d.get("temp_max_c")),
            temp_min_c=_opt_float(d.get("temp_min_c")),
            max_valid_utc=parse_utc(d.get("max_valid_utc")),
            min_valid_utc=parse_utc(d.get("min_valid_utc")),
            complete=bool(d.get("complete", d.get("local_day_complete", False))),
            status=str(d.get("status") or ""),
            receipt_utc=parse_utc(d.get("receipt_utc") or d.get("receipt_time")),
            hours_available=_opt_int(d.get("hours_available", d.get("available_through_forecast_hour"))),
            max_delta_c=_opt_float((d.get("change") or {}).get("temp_max_delta_c") if isinstance(d.get("change"), dict) else d.get("max_delta_c")),
            min_delta_c=_opt_float((d.get("change") or {}).get("temp_min_delta_c") if isinstance(d.get("change"), dict) else d.get("min_delta_c")),
            previous_run_utc=parse_utc((d.get("change") or {}).get("previous_run_utc") if isinstance(d.get("change"), dict) else d.get("previous_run_utc")),
        )


def _opt_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _opt_int(v: Any) -> Optional[int]:
    f = _opt_float(v)
    return int(f) if f is not None else None


def forecast_channel_tier(channel: str) -> str:
    parts = (channel or "").split(".")
    return parts[2] if len(parts) >= 4 and parts[0] == "metar" and parts[1] == "forecast" else ""


def normalize_forecast_publication(msg: dict[str, Any]) -> Optional[ForecastRecord]:
    """``publication`` frame on a ``metar.forecast.*`` channel -> record, else None."""
    if not isinstance(msg, dict) or msg.get("type") != "publication":
        return None
    channel = str(msg.get("channel") or "")
    if not forecast_channel_tier(channel):
        return None
    data = msg.get("data")
    if not isinstance(data, dict):
        return None
    d = dict(data)
    if not d.get("station"):
        d["station"] = channel.split(".")[-1].upper()
    if not d.get("tier"):
        d["tier"] = forecast_channel_tier(channel)
    return ForecastRecord.from_dict(d)


class ForecastTracker:
    """Latest run per (station, local day, model)."""

    def __init__(self, *, keep_days: int = 5) -> None:
        self.keep_days = max(1, keep_days)
        self._by_key: dict[tuple[str, str], dict[str, ForecastRecord]] = {}
        self.records_seen = 0

    def ingest(self, rec: ForecastRecord) -> bool:
        """Store when newer than the known run for that model/day; returns True if stored."""
        models = self._by_key.setdefault((rec.station, rec.local_date), {})
        cur = models.get(rec.model)
        if cur is not None and cur.run_utc > rec.run_utc:
            return False
        if cur is not None and cur.run_utc == rec.run_utc and cur.complete and not rec.complete:
            return False
        models[rec.model] = rec
        self.records_seen += 1
        return True

    def get(self, icao: str, local_day: date | str) -> dict[str, ForecastRecord]:
        d = local_day.isoformat() if isinstance(local_day, date) else str(local_day)
        return dict(self._by_key.get(((icao or "").upper(), d), {}))

    def days_for(self, icao: str) -> list[str]:
        code = (icao or "").upper()
        return sorted(d for (i, d) in self._by_key if i == code)

    def prune(self, today_utc: Optional[date] = None) -> int:
        today = today_utc or datetime.now(timezone.utc).date()
        removed = 0
        for key in list(self._by_key):
            try:
                d = date.fromisoformat(key[1])
            except ValueError:
                d = today
            if (today - d).days > self.keep_days:
                del self._by_key[key]
                removed += 1
        return removed

    def to_dict(self) -> dict[str, dict[str, dict[str, Any]]]:
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for (icao, day), models in sorted(self._by_key.items()):
            out.setdefault(icao, {})[day] = {m: r.to_dict() for m, r in sorted(models.items())}
        return out

    def restore(self, payload: Any) -> int:
        if not isinstance(payload, dict):
            return 0
        n = 0
        for icao, by_day in payload.items():
            if not isinstance(by_day, dict):
                continue
            for day, models in by_day.items():
                if not isinstance(models, dict):
                    continue
                for row in models.values():
                    if isinstance(row, dict):
                        rec = ForecastRecord.from_dict(row)
                        if rec is not None and self.ingest(rec):
                            n += 1
        return n

    def __len__(self) -> int:
        return sum(len(m) for m in self._by_key.values())


def forecasts_from_snapshot(snapshot: dict[str, Any], icao: str, local_day: date | str) -> dict[str, ForecastRecord]:
    fc = snapshot.get("forecasts") if isinstance(snapshot, dict) else None
    if not isinstance(fc, dict):
        return {}
    d = local_day.isoformat() if isinstance(local_day, date) else str(local_day)
    models = (fc.get((icao or "").upper()) or {}).get(d) or {}
    out: dict[str, ForecastRecord] = {}
    for m, row in models.items():
        if isinstance(row, dict):
            rec = ForecastRecord.from_dict(row)
            if rec is not None:
                out[m] = rec
    return out


# -- probability ---------------------------------------------------------------------

def celsius_interval_for_bucket(low: Optional[float], high: Optional[float], unit: str) -> tuple[float, float]:
    """Celsius interval ``[a, b)`` whose values round (halves up) into the bucket of the market unit."""
    a = -math.inf if low is None else low - 0.5
    b = math.inf if high is None else high + 0.5
    if not (unit or "F").upper().startswith("C"):
        a = -math.inf if a == -math.inf else (a - 32.0) * 5.0 / 9.0
        b = math.inf if b == math.inf else (b - 32.0) * 5.0 / 9.0
    return a, b


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class BucketProbability:
    p: float
    models: int
    mean_c: Optional[float]
    spread_c: Optional[float]
    note: str
    components: list[dict[str, Any]] = field(default_factory=list)


def bucket_probability(
    *,
    variable: str,
    low: Optional[float],
    high: Optional[float],
    unit: str,
    forecasts: Iterable[ForecastRecord],
    observed_extreme_c: Optional[float],
    now_utc: Optional[datetime] = None,
) -> Optional[BucketProbability]:
    """P(resolution value in bucket) from a normal mixture over model forecasts, conditioned on observations."""
    now = now_utc or datetime.now(timezone.utc)
    rises = (variable or "temp_max").strip().lower() != "temp_min"
    a, b = celsius_interval_for_bucket(low, high, unit)
    sig_hi = _env_float("FORECAST_SIGMA_HIRES_C", 1.3)
    sig_basic = _env_float("FORECAST_SIGMA_BASIC_C", 1.9)
    sig_late = _env_float("FORECAST_SIGMA_LATE_C", 0.6)
    peak_hours = _env_float("FORECAST_PEAK_PASSED_HOURS", 2.0)

    comps: list[tuple[float, float, float, dict[str, Any]]] = []  # (weight, mean, sigma, info)
    partial_bound: Optional[float] = None  # a run covering part of the day only bounds the extreme
    for rec in forecasts:
        val = rec.temp_max_c if rises else rec.temp_min_c
        if val is None:
            continue
        if not rec.complete:
            # a partial run's max/min is the extreme of the hours it covers, not of the day:
            # the day's max is at least that (min at most), nothing more
            partial_bound = val if partial_bound is None else (max(partial_bound, val) if rises else min(partial_bound, val))
            continue
        sigma = sig_hi if rec.tier == "hires" else sig_basic
        valid = rec.max_valid_utc if rises else rec.min_valid_utc
        late = valid is not None and now > valid + timedelta(hours=peak_hours) and observed_extreme_c is not None
        mean = val
        if late:
            mean, sigma = float(observed_extreme_c), sig_late
        comps.append((1.0, mean, sigma, {"model": rec.model, "tier": rec.tier, "value_c": val, "sigma_c": round(sigma, 2), "run_utc": format_utc(rec.run_utc), "late": late}))
    if not comps:
        return None
    if partial_bound is not None:
        # fold the partial-run bound into the observation bound (same direction, same meaning)
        if observed_extreme_c is None:
            observed_extreme_c = partial_bound
        else:
            observed_extreme_c = max(observed_extreme_c, partial_bound) if rises else min(observed_extreme_c, partial_bound)

    total_w = sum(c[0] for c in comps)
    p = 0.0
    for w, mean, sigma, _ in comps:
        lo, hi = a, b
        if observed_extreme_c is not None:
            if rises:
                if hi <= observed_extreme_c:  # bucket entirely below what was already observed
                    continue
                lo = max(lo, observed_extreme_c)
                # X = max(observed, Y): P(X in [lo,hi)) = P(Y < hi) - P(Y < lo) for lo > observed,
                # and P(Y < hi) when the bucket contains the observed value
                if a <= observed_extreme_c:
                    part = _cdf(hi, mean, sigma)
                else:
                    part = _cdf(hi, mean, sigma) - _cdf(lo, mean, sigma)
            else:
                if lo >= observed_extreme_c:  # bucket entirely above the observed min
                    continue
                hi = min(hi, observed_extreme_c)
                if b > observed_extreme_c:
                    part = 1.0 - _cdf(lo, mean, sigma)
                else:
                    part = _cdf(hi, mean, sigma) - _cdf(lo, mean, sigma)
        else:
            part = _cdf(hi, mean, sigma) - _cdf(lo, mean, sigma)
        p += w * max(0.0, min(1.0, part))
    p = max(0.0, min(1.0, p / total_w))

    vals = [c[1] for c in comps]
    mean_c = sum(c[0] * c[1] for c in comps) / total_w
    spread = (max(vals) - min(vals)) if len(vals) > 1 else 0.0
    note = f"{len(comps)} model(s), mixture sigma per tier, conditioned on observed {'max' if rises else 'min'}"
    return BucketProbability(p=round(p, 4), models=len(comps), mean_c=round(mean_c, 2), spread_c=round(spread, 2), note=note, components=[c[3] for c in comps])


def _cdf(x: float, mean: float, sigma: float) -> float:
    if x == math.inf:
        return 1.0
    if x == -math.inf:
        return 0.0
    return _ncdf((x - mean) / max(sigma, 1e-6))


__all__ = [
    "BucketProbability",
    "ForecastRecord",
    "ForecastTracker",
    "bucket_probability",
    "celsius_interval_for_bucket",
    "forecast_channel_tier",
    "forecasts_from_snapshot",
    "normalize_forecast_publication",
]
