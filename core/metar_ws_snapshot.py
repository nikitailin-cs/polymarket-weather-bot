"""
Snapshot bridge between the long-lived METAR.ws daemon and the one-shot signal
producers (``produce_rt_signals.py`` of bot1 / bot2).

The daemon writes ``metar_ws_latest.json`` atomically on every observation and
on a heartbeat; the producers read it through :class:`MetarWsSnapshotRegistry`,
which exposes the two historical registry interfaces:

- ``fetch_latest_for_stations(icaos) -> dict[ICAO, MetarObservation]`` (bot1);
- ``fetch_latest_metar(icao) -> dict | None`` (bot2).

Environment:

- ``METAR_WS_SNAPSHOT_FILE``        default ``data/bots/realtime/metar_ws_latest.json``
- ``METAR_WS_SNAPSHOT_MAX_AGE_SEC`` daemon liveness threshold (default 180): an older
                                    snapshot is treated as stale
- ``METAR_WS_OBS_MAX_AGE_SEC``      per-station observation age limit (default 7200,
                                    matching the AWC ``hours=2`` window; 0 disables)
- ``METAR_WS_FALLBACK_AWC``         ``1`` to fall back to the AWC polling registry for
                                    stations missing from a fresh snapshot, or for all
                                    stations when the snapshot is stale (default ``0``)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from core.metar_observation import MetarObservation, format_utc, parse_utc

LOG = logging.getLogger("metar_ws.snapshot")

SNAPSHOT_SCHEMA = "metar_ws_snapshot.v1"
_REPO = Path(__file__).resolve().parent.parent


def snapshot_path() -> Path:
    raw = (os.getenv("METAR_WS_SNAPSHOT_FILE") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (_REPO / p).resolve()
    return (_REPO / "data" / "bots" / "realtime" / "metar_ws_latest.json").resolve()


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def write_snapshot_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def build_snapshot(
    latest: dict[str, MetarObservation],
    *,
    connected: bool,
    state: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    days: Optional[dict[str, dict[str, Any]]] = None,
    forecasts: Optional[dict[str, Any]] = None,
    fast: Optional[dict[str, MetarObservation]] = None,
    sources: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    stations = {icao: obs.to_dict() for icao, obs in sorted(latest.items())}
    payload: dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at_utc": format_utc(now),
        "connected": bool(connected),
        "stations": stations,
        "days": days or {},
        "forecasts": forecasts or {},
        "fast": {icao: obs.to_dict() for icao, obs in sorted((fast or {}).items())},
        "sources": sources or {},
    }
    if state:
        payload["client"] = state
    return payload


def read_snapshot(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        LOG.warning("snapshot unreadable (%s): %s", path, e)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("stations"), dict):
        LOG.warning("snapshot has unexpected shape (%s)", path)
        return None
    return data


class MetarWsSnapshotRegistry:
    """
    Registry backed by the daemon snapshot, with optional AWC fallback.

    ``fallback`` is any object exposing ``fetch_latest_for_stations`` (bot1 AWC
    registry) and/or ``fetch_latest_metar`` (bot2 AWC registry).
    """

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        max_age_sec: Optional[int] = None,
        obs_max_age_sec: Optional[int] = None,
        fallback: Any = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.path = path or snapshot_path()
        self.max_age_sec = max_age_sec if max_age_sec is not None else _env_int(
            "METAR_WS_SNAPSHOT_MAX_AGE_SEC", 180
        )
        self.obs_max_age_sec = (
            obs_max_age_sec if obs_max_age_sec is not None else _env_int("METAR_WS_OBS_MAX_AGE_SEC", 7200)
        )
        self.fallback = fallback
        self._now = now_fn
        self.last_status: str = "unread"

    # -- snapshot access --------------------------------------------------------

    def load(self) -> dict[str, MetarObservation]:
        """Fresh observations from the snapshot; empty dict when missing or stale."""
        now = self._now()
        snap = read_snapshot(self.path)
        if snap is None:
            self.last_status = "missing"
            LOG.warning("METAR.ws snapshot missing: %s", self.path)
            return {}
        gen = parse_utc(snap.get("generated_at_utc"))
        if gen is None or (now - gen).total_seconds() > self.max_age_sec:
            self.last_status = "stale"
            LOG.warning(
                "METAR.ws snapshot stale (generated %s, max age %ss): %s",
                snap.get("generated_at_utc"),
                self.max_age_sec,
                self.path,
            )
            return {}
        self.last_status = "ok" if snap.get("connected", True) else "disconnected"
        out: dict[str, MetarObservation] = {}
        for icao, row in snap["stations"].items():
            if not isinstance(row, dict):
                continue
            obs = MetarObservation.from_dict(row)
            if obs is None:
                continue
            if self.obs_max_age_sec > 0 and (now - obs.observation_utc).total_seconds() > self.obs_max_age_sec:
                LOG.info("observation for %s too old (%s), dropped", icao, obs.report_time_utc)
                continue
            out[str(icao).upper()] = obs
        return out

    def fetch_day_state(self, icao: str, local_day: Any):
        """
        Running daily extremes for ``icao`` on ``local_day`` (``date`` or ``YYYY-MM-DD``),
        or ``None`` when the snapshot is missing/stale or the day is unknown.
        """
        from core.station_day_state import day_state_from_snapshot

        snap = read_snapshot(self.path)
        if snap is None:
            self.last_status = "missing"
            return None
        gen = parse_utc(snap.get("generated_at_utc"))
        if gen is None or (self._now() - gen).total_seconds() > self.max_age_sec:
            self.last_status = "stale"
            return None
        return day_state_from_snapshot(snap, icao, local_day)

    def fetch_forecasts(self, icao: str, local_day: Any) -> dict[str, Any]:
        """Latest model forecasts for ``icao`` on ``local_day`` (model -> ForecastRecord); {} when stale/missing."""
        from core.forecast_state import forecasts_from_snapshot

        snap = read_snapshot(self.path)
        if snap is None:
            return {}
        gen = parse_utc(snap.get("generated_at_utc"))
        if gen is None or (self._now() - gen).total_seconds() > self.max_age_sec:
            return {}
        return forecasts_from_snapshot(snap, icao, local_day)

    # -- bot1 interface ---------------------------------------------------------

    def fetch_latest_for_stations(self, icao_list: Iterable[str]) -> dict[str, MetarObservation]:
        ids = sorted({str(x).strip().upper() for x in icao_list if str(x).strip()})
        if not ids:
            return {}
        fresh = self.load()
        out = {i: fresh[i] for i in ids if i in fresh}
        missing = [i for i in ids if i not in out]
        if missing and self.fallback is not None and hasattr(self.fallback, "fetch_latest_for_stations"):
            LOG.info("AWC fallback for %s (snapshot %s)", ", ".join(missing), self.last_status)
            try:
                out.update(self.fallback.fetch_latest_for_stations(missing))
            except Exception as e:  # noqa: BLE001
                LOG.error("AWC fallback failed: %s", e)
        elif missing:
            LOG.warning("no METAR.ws observation for: %s (snapshot %s)", ", ".join(missing), self.last_status)
        return out

    # -- bot2 interface ---------------------------------------------------------

    def fetch_latest_metar(self, icao_code: str) -> Optional[dict[str, Any]]:
        code = (icao_code or "").strip().upper()
        if not code:
            return None
        obs = self.load().get(code)
        if obs is not None:
            return obs.to_bot2_dict()
        if self.fallback is not None and hasattr(self.fallback, "fetch_latest_metar"):
            LOG.info("AWC fallback for %s (snapshot %s)", code, self.last_status)
            try:
                return self.fallback.fetch_latest_metar(code)
            except Exception as e:  # noqa: BLE001
                LOG.error("AWC fallback failed: %s", e)
                return None
        LOG.warning("no METAR.ws observation for %s (snapshot %s)", code, self.last_status)
        return None
