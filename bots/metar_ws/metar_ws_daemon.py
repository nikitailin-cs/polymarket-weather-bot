#!/usr/bin/env python3
"""
Real-time data daemon: hosts the configured data sources (``SOURCES``, default
``metar_ws``), keeps the per-station daily extremes and model forecasts, writes
the snapshot read by the RT producers and (optionally) triggers a trading cycle
the moment a live routine report arrives.

    python bots/metar_ws/metar_ws_daemon.py                 # run forever
    python bots/metar_ws/metar_ws_daemon.py --stations KNYC,KORD --max-seconds 90
    SOURCES=metar_ws,awc_poll python bots/metar_ws/metar_ws_daemon.py

Environment (in addition to the sources' own variables):

- ``SOURCES``                         comma-separated source names (``metar_ws``, ``awc_poll``)
- ``UPDATE_BOT_RT_PIPELINE``          ``bot2`` (default) or ``bot1``: which mapping path to use
- ``METAR_WS_MAPPING_RELOAD_SEC``     re-read the mapping to pick up new stations (60)
- ``METAR_WS_SNAPSHOT_HEARTBEAT_SEC`` rewrite the snapshot even without traffic (30)
- ``METAR_WS_TRIGGER_CMD``            shell command run after a *live routine* observation, e.g.
                                      ``python bots/metar_ws/run_cycle.py`` (empty = no trigger)
- ``METAR_WS_TRIGGER_DEBOUNCE_SEC``   coalesce bursts of reports into one run (5)
- ``METAR_WS_TRIGGER_ON_SPECI_ONLY``  ``1`` to trigger only on SPECI reports (default ``0``)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.forecast_state import ForecastRecord, ForecastTracker  # noqa: E402
from core.metar_observation import MetarObservation, format_utc  # noqa: E402
from core.metar_ws_snapshot import build_snapshot, read_snapshot, snapshot_path, write_snapshot_atomic  # noqa: E402
from core.sources import Sink, Source, build_sources, source_names_from_env  # noqa: E402
from core.station_day_state import DayStateTracker  # noqa: E402
from ui.history import EVENTS_FILE, append_jsonl  # noqa: E402

LOG = logging.getLogger("metar_ws.daemon")


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


# -- station registries -----------------------------------------------------------------

def mapping_path_for_pipeline(pipeline: str) -> Path:
    if pipeline == "bot1":
        from core.bot1_realtime_data_paths import icao_mapping_path

        return icao_mapping_path()
    from core.bot2_data_paths import get_rt_paths

    return get_rt_paths()["mapping"]


def _load_stations_file(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    path = path or (_ROOT / "data" / "metar_ws_stations.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k).upper(): v for k, v in (data.get("stations") or {}).items() if isinstance(v, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def served_stations(path: Optional[Path] = None) -> set[str]:
    """ICAO codes known to be served by stream.metar.ws (data/metar_ws_stations.json)."""
    return set(_load_stations_file(path))


def served_timezones(path: Optional[Path] = None) -> dict[str, str]:
    return {k: str(v["timezone"]) for k, v in _load_stations_file(path).items() if v.get("timezone")}


def fast_stations(path: Optional[Path] = None) -> set[str]:
    """Stations with a frequent-observation channel (``obs10`` flag)."""
    return {k for k, v in _load_stations_file(path).items() if v.get("obs10")}


def timezones_from_mapping(path: Path) -> dict[str, str]:
    """ICAO -> IANA timezone from the mapping entries (first non-empty value wins)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    if not isinstance(data, dict):
        return out
    for key, cfg in data.items():
        if not isinstance(key, str) or key.startswith("_") or not isinstance(cfg, dict):
            continue
        icao = str(cfg.get("icao") or "").strip().upper()
        tz = str(cfg.get("timezone") or "").strip()
        if len(icao) == 4 and tz and icao not in out:
            out[icao] = tz
    return out


def stations_from_mapping(path: Path) -> set[str]:
    """ICAO codes of every market entry (keys starting with ``_`` are notes)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        LOG.warning("mapping not found: %s", path)
        return set()
    except (OSError, ValueError) as e:
        LOG.error("mapping unreadable (%s): %s", path, e)
        return set()
    out: set[str] = set()
    if not isinstance(data, dict):
        return out
    for key, cfg in data.items():
        if not isinstance(key, str) or key.startswith("_") or not isinstance(cfg, dict):
            continue
        icao = str(cfg.get("icao") or "").strip().upper()
        if len(icao) == 4:
            out.add(icao)
    return out


# -- daemon -----------------------------------------------------------------------------

class Daemon:
    def __init__(
        self,
        *,
        sources: Optional[list[Source]] = None,
        mapping_path: Optional[Path] = None,
        fixed_stations: Optional[set[str]] = None,
        snapshot_file: Optional[Path] = None,
        trigger_cmd: str = "",
        trigger_debounce_sec: float = 5.0,
        trigger_speci_only: bool = False,
        mapping_reload_sec: float = 60.0,
        heartbeat_sec: float = 30.0,
        events_file: Optional[Path] = None,
        stations_file: Optional[Path] = None,
    ) -> None:
        self.events_file = events_file or EVENTS_FILE
        self.mapping_path = mapping_path
        self.fixed_stations = fixed_stations
        self.snapshot_file = snapshot_file or snapshot_path()
        self.trigger_cmd = trigger_cmd.strip()
        self.trigger_debounce_sec = max(0.0, trigger_debounce_sec)
        self.trigger_speci_only = trigger_speci_only
        self.mapping_reload_sec = max(5.0, mapping_reload_sec)
        self.heartbeat_sec = max(5.0, heartbeat_sec)
        self._stations_file = stations_file
        self._tz_by_icao: dict[str, str] = {}
        self._served_tz = served_timezones(stations_file)
        self._fast = fast_stations(stations_file)
        self.sources: list[Source] = sources if sources is not None else build_sources(source_names_from_env())
        for s in self.sources:
            if hasattr(s, "set_fast_stations"):
                s.set_fast_stations(self._fast)
        self.sink = Sink(on_observation=self._on_observation, on_forecast=self._on_forecast, on_state_change=self._on_state_change)
        self.days = DayStateTracker(lambda icao: self._tz_by_icao.get(icao) or self._served_tz.get(icao, "UTC"), keep_days=3)
        self.forecasts = ForecastTracker()
        self.latest: dict[str, MetarObservation] = {}  # newest routine report per station (any source)
        self.latest_fast: dict[str, MetarObservation] = {}  # newest obs10 per station
        self.live_observations = 0
        self.triggers_run = 0
        self._trigger_pending: Optional[asyncio.Task] = None
        self._trigger_running: Optional[asyncio.subprocess.Process] = None
        self._trigger_rerun = False
        self._last_connected: Optional[bool] = None
        self._restore()

    # -- state ------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return any(s.state().connected for s in self.sources)

    def _primary(self) -> Optional[Source]:
        """The push source whose client state the snapshot ``client`` section mirrors."""
        for s in self.sources:
            if s.name == "metar_ws":
                return s
        return self.sources[0] if self.sources else None

    def _client_state(self) -> dict[str, Any]:
        primary = self._primary()
        st = primary.state() if primary else None
        d: dict[str, Any] = dict(st.details) if st else {}
        d.update({
            "plan": d.get("plan", ""),
            "limits": d.get("limits", {}),
            "connected_since_utc": d.get("connected_since_utc", ""),
            "last_message_at_utc": st.to_dict()["last_message_at_utc"] if st else "",
            "reconnects": d.get("reconnects", 0),
            "messages": st.messages if st else 0,
            "subscribed": d.get("subscribed", []),
            "wanted_channels": d.get("wanted_channels", []),
            "failed_channels": d.get("failed_channels", {}),
            "last_error": st.last_error if st else "",
            "live_observations": self.live_observations,
            "triggers_run": self.triggers_run,
            "station_days_tracked": len(self.days),
            "forecast_records": len(self.forecasts),
            "forecasts_received": d.get("forecasts_received", 0),
            "fast_observations": d.get("fast_observations", 0),
            "recent_latencies": d.get("recent_latencies", []),
            "platform_latency_ms": d.get("platform_latency_ms"),
            "delivery_latency_ms": d.get("delivery_latency_ms"),
            "sources": [s.name for s in self.sources],
        })
        return d

    def _restore(self) -> None:
        snap = read_snapshot(self.snapshot_file)
        if snap is None:
            return
        n = self.days.restore(snap.get("days"))
        if n:
            LOG.info("restored %s station-day state(s) from %s", n, self.snapshot_file)
        nf = self.forecasts.restore(snap.get("forecasts"))
        if nf:
            LOG.info("restored %s forecast record(s)", nf)
        for section, target in (("stations", self.latest), ("fast", self.latest_fast)):
            for icao, row in (snap.get(section) or {}).items():
                obs = MetarObservation.from_dict(row) if isinstance(row, dict) else None
                if obs is not None:
                    target.setdefault(str(icao).upper(), obs)

    def _write_snapshot(self) -> None:
        state = self._client_state()
        connected = self.connected and bool(state.get("plan") or any(s.kind == "poll" for s in self.sources))
        if connected != self._last_connected and not (self._last_connected is None and not connected):
            append_jsonl(self.events_file, {"kind": "connected" if connected else "disconnected",
                                            "plan": state.get("plan", ""), "error": state.get("last_error", "")})
        self._last_connected = connected
        try:
            payload = build_snapshot(
                self.latest,
                connected=self.connected,
                state=state,
                days=self.days.to_dict(),
                forecasts=self.forecasts.to_dict(),
                fast=self.latest_fast,
                sources={s.name: s.state().to_dict() for s in self.sources},
            )
            write_snapshot_atomic(self.snapshot_file, payload)
        except OSError as e:
            LOG.error("snapshot write failed (%s): %s", self.snapshot_file, e)

    # -- sink callbacks -------------------------------------------------------------------

    async def _on_state_change(self, source: str) -> None:
        self._write_snapshot()

    async def _on_forecast(self, rec: ForecastRecord, cached: bool, source: str) -> None:
        if self.forecasts.ingest(rec):
            self._write_snapshot()
            if not cached:
                append_jsonl(self.events_file, {"kind": "forecast", "icao": rec.station, "model": rec.model, "local_date": rec.local_date,
                                                "temp_max_c": rec.temp_max_c, "temp_min_c": rec.temp_min_c, "run_utc": format_utc(rec.run_utc), "source": source})

    async def _on_observation(self, obs: MetarObservation, cached: bool, source: str) -> None:
        st = self.days.ingest(obs)
        LOG.debug("%s %s %s day max=%s min=%s n=%s", source, obs.icao_id, st.local_date, st.max_c, st.min_c, st.obs_count)
        if obs.report_kind == "obs10":
            cur = self.latest_fast.get(obs.icao_id)
            if cur is None or obs.observation_utc >= cur.observation_utc:
                self.latest_fast[obs.icao_id] = obs
        else:
            cur = self.latest.get(obs.icao_id)
            if cur is None or obs.observation_utc > cur.observation_utc or (obs.observation_utc == cur.observation_utc and obs.is_speci and not cur.is_speci):
                self.latest[obs.icao_id] = obs
        self._write_snapshot()
        if cached:
            return
        self.live_observations += 1
        append_jsonl(self.events_file, {"kind": "observation", "icao": obs.icao_id, "temp_c": obs.temp_celsius,
                                        "obs_time": obs.report_time_utc, "speci": obs.is_speci, "report": obs.report_kind, "source": source})
        if not self.trigger_cmd or obs.report_kind == "obs10":
            return  # frequent observations never trigger a cycle: they are not resolution data
        if self.trigger_speci_only and not obs.is_speci:
            return
        self._schedule_trigger(reason=f"{obs.icao_id} {obs.report_time_utc}")

    # -- trigger ----------------------------------------------------------------------------

    def _schedule_trigger(self, *, reason: str) -> None:
        if self._trigger_pending is not None and not self._trigger_pending.done():
            LOG.debug("trigger already pending (%s)", reason)
            return
        self._trigger_pending = asyncio.ensure_future(self._run_trigger_after_debounce(reason))

    async def _run_trigger_after_debounce(self, reason: str) -> None:
        if self.trigger_debounce_sec > 0:
            await asyncio.sleep(self.trigger_debounce_sec)
        if self._trigger_running is not None and self._trigger_running.returncode is None:
            LOG.info("trigger still running; will rerun after it finishes (%s)", reason)
            self._trigger_rerun = True
            return
        await self._run_trigger(reason)
        while self._trigger_rerun:
            self._trigger_rerun = False
            await self._run_trigger("rerun after overlapping observation")

    async def _run_trigger(self, reason: str) -> None:
        LOG.info("trigger: %s (%s)", self.trigger_cmd, reason)
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_shell(self.trigger_cmd, cwd=str(_ROOT))
        except OSError as e:
            LOG.error("trigger failed to start: %s", e)
            return
        self._trigger_running = proc
        rc = await proc.wait()
        self._trigger_running = None
        self.triggers_run += 1
        LOG.info("trigger finished rc=%s in %.1fs", rc, time.monotonic() - started)
        append_jsonl(self.events_file, {"kind": "trigger", "reason": reason, "rc": rc, "seconds": round(time.monotonic() - started, 1)})
        self._write_snapshot()

    # -- station list -------------------------------------------------------------------------

    def _refresh_stations(self) -> None:
        if self.mapping_path is not None:
            self._tz_by_icao.update(timezones_from_mapping(self.mapping_path))
        if self.fixed_stations is not None:
            wanted = set(self.fixed_stations)
        elif self.mapping_path is not None:
            wanted = stations_from_mapping(self.mapping_path)
        else:
            wanted = set()
        current = self.sources[0].stations if self.sources else set()
        if wanted != current:
            LOG.info("stations: %s", ", ".join(sorted(wanted)) or "(none)")
            known = served_stations(self._stations_file)
            unknown = sorted(wanted - known) if known else []
            if unknown:
                LOG.warning("not in data/metar_ws_stations.json (check the market's resolution station): %s", ", ".join(unknown))
            for s in self.sources:
                s.set_stations(wanted)
            self._write_snapshot()

    async def _housekeeping(self, max_seconds: Optional[float]) -> None:
        started = time.monotonic()
        next_reload = started + self.mapping_reload_sec
        next_beat = started + self.heartbeat_sec
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            if max_seconds is not None and now - started >= max_seconds:
                LOG.info("max-seconds reached, stopping")
                self.stop()
                return
            if now >= next_reload:
                next_reload = now + self.mapping_reload_sec
                self._refresh_stations()
            if now >= next_beat:
                next_beat = now + self.heartbeat_sec
                self.days.prune()
                self.forecasts.prune()
                self._write_snapshot()

    def stop(self) -> None:
        for s in self.sources:
            s.stop()

    # -- main ---------------------------------------------------------------------------------

    async def run(self, *, max_seconds: Optional[float] = None) -> int:
        if not self.sources:
            LOG.error("no sources configured (SOURCES)")
            return 2
        LOG.info("sources: %s", ", ".join(s.name for s in self.sources))
        self._refresh_stations()
        self._write_snapshot()
        hk = asyncio.ensure_future(self._housekeeping(max_seconds))
        try:
            await asyncio.gather(*(s.run(self.sink) for s in self.sources))
        finally:
            hk.cancel()
            try:
                await hk
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            if self._trigger_pending is not None and not self._trigger_pending.done():
                self._trigger_pending.cancel()
            self._write_snapshot()
        return 0


def _install_signal_handlers(daemon: Daemon) -> None:
    loop = asyncio.get_running_loop()

    def _stop(*_: Any) -> None:
        LOG.info("stop requested")
        daemon.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except (NotImplementedError, RuntimeError):  # Windows / non-main thread
            signal.signal(sig, lambda *_: _stop())


async def _amain(args: argparse.Namespace) -> int:
    pipeline = (os.getenv("UPDATE_BOT_RT_PIPELINE") or "bot2").strip().lower()
    fixed = None
    if args.stations:
        fixed = {s.strip().upper() for s in args.stations.split(",") if s.strip()}
    names = [n.strip() for n in args.sources.split(",") if n.strip()] if args.sources else source_names_from_env()
    daemon = Daemon(
        sources=build_sources(names),
        mapping_path=mapping_path_for_pipeline(pipeline),
        fixed_stations=fixed,
        trigger_cmd=(os.getenv("METAR_WS_TRIGGER_CMD") or "") if not args.no_trigger else "",
        trigger_debounce_sec=_env_float("METAR_WS_TRIGGER_DEBOUNCE_SEC", 5.0),
        trigger_speci_only=(os.getenv("METAR_WS_TRIGGER_ON_SPECI_ONLY") or "0").strip() == "1",
        mapping_reload_sec=_env_float("METAR_WS_MAPPING_RELOAD_SEC", 60.0),
        heartbeat_sec=_env_float("METAR_WS_SNAPSHOT_HEARTBEAT_SEC", 30.0),
    )
    _install_signal_handlers(daemon)
    LOG.info("pipeline=%s snapshot=%s trigger=%r", pipeline, daemon.snapshot_file, daemon.trigger_cmd or None)
    return await daemon.run(max_seconds=args.max_seconds)


def main() -> int:
    p = argparse.ArgumentParser(description="Real-time data daemon (sources -> snapshot -> cycle trigger)")
    p.add_argument("--stations", default="", help="comma-separated ICAO list (overrides the mapping)")
    p.add_argument("--sources", default="", help="comma-separated source names (overrides SOURCES)")
    p.add_argument("--max-seconds", type=float, default=None, help="exit after N seconds (smoke test)")
    p.add_argument("--no-trigger", action="store_true", help="ignore METAR_WS_TRIGGER_CMD")
    p.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = p.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    try:
        from dotenv import load_dotenv

        load_dotenv(_ROOT / ".env")
    except Exception:  # noqa: BLE001 - python-dotenv optional
        pass
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
