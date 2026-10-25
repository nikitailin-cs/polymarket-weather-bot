#!/usr/bin/env python3
"""
METAR.ws daemon: one persistent WebSocket subscription for every station in
``icao_mapping.json``; writes the observation snapshot read by the RT producers
and (optionally) triggers a trading cycle the moment a live report arrives.

    python bots/metar_ws/metar_ws_daemon.py                 # run forever
    python bots/metar_ws/metar_ws_daemon.py --stations KNYC,KORD --max-seconds 90

Environment (in addition to ``core/metar_ws_client.py`` / ``core/metar_ws_snapshot.py``):

- ``UPDATE_BOT_RT_PIPELINE``          ``bot2`` (default) or ``bot1``: which mapping path to use
- ``METAR_WS_MAPPING_RELOAD_SEC``     re-read the mapping to pick up new stations (60)
- ``METAR_WS_SNAPSHOT_HEARTBEAT_SEC`` rewrite the snapshot even without traffic (30)
- ``METAR_WS_TRIGGER_CMD``            shell command run after a *live* observation, e.g.
                                      ``bash update_bot_rt.sh`` (empty = no trigger)
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

from core.metar_observation import MetarObservation, format_utc  # noqa: E402
from core.metar_ws_client import MetarWsClient, MetarWsConfig  # noqa: E402
from core.metar_ws_snapshot import build_snapshot, read_snapshot, snapshot_path, write_snapshot_atomic  # noqa: E402
from core.forecast_state import ForecastRecord, ForecastTracker  # noqa: E402
from core.station_day_state import DayStateTracker  # noqa: E402
from ui.history import EVENTS_FILE, append_jsonl  # noqa: E402

LOG = logging.getLogger("metar_ws.daemon")


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def mapping_path_for_pipeline(pipeline: str) -> Path:
    if pipeline == "bot1":
        from core.bot1_realtime_data_paths import icao_mapping_path

        return icao_mapping_path()
    from core.bot2_data_paths import get_rt_paths

    return get_rt_paths()["mapping"]


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


def served_stations(path: Optional[Path] = None) -> set[str]:
    """ICAO codes known to be served by stream.metar.ws (data/metar_ws_stations.json)."""
    path = path or (_ROOT / "data" / "metar_ws_stations.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k).upper() for k in (data.get("stations") or {})}
    except (OSError, ValueError, AttributeError):
        return set()


def served_timezones(path: Optional[Path] = None) -> dict[str, str]:
    """ICAO -> IANA timezone from data/metar_ws_stations.json."""
    path = path or (_ROOT / "data" / "metar_ws_stations.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k).upper(): str(v["timezone"]) for k, v in (data.get("stations") or {}).items() if isinstance(v, dict) and v.get("timezone")}
    except (OSError, ValueError, AttributeError):
        return {}


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


class Daemon:
    def __init__(
        self,
        *,
        config: Optional[MetarWsConfig] = None,
        mapping_path: Optional[Path] = None,
        fixed_stations: Optional[set[str]] = None,
        snapshot_file: Optional[Path] = None,
        trigger_cmd: str = "",
        trigger_debounce_sec: float = 5.0,
        trigger_speci_only: bool = False,
        mapping_reload_sec: float = 60.0,
        heartbeat_sec: float = 30.0,
        events_file: Optional[Path] = None,
    ) -> None:
        self.events_file = events_file or EVENTS_FILE
        self.config = config or MetarWsConfig.from_env()
        self.mapping_path = mapping_path
        self.fixed_stations = fixed_stations
        self.snapshot_file = snapshot_file or snapshot_path()
        self.trigger_cmd = trigger_cmd.strip()
        self.trigger_debounce_sec = max(0.0, trigger_debounce_sec)
        self.trigger_speci_only = trigger_speci_only
        self.mapping_reload_sec = max(5.0, mapping_reload_sec)
        self.heartbeat_sec = max(5.0, heartbeat_sec)
        self.client = MetarWsClient(
            self.config,
            on_observation=self._on_observation,
            on_state_change=self._write_snapshot,
            on_forecast=self._on_forecast,
        )
        self.forecasts = ForecastTracker()
        self._tz_by_icao: dict[str, str] = {}
        self._served_tz = served_timezones()
        self.days = DayStateTracker(lambda icao: self._tz_by_icao.get(icao) or self._served_tz.get(icao, "UTC"), keep_days=3)
        self._restore_days()
        self.live_observations = 0
        self.triggers_run = 0
        self._trigger_pending: Optional[asyncio.Task] = None
        self._trigger_running: Optional[asyncio.subprocess.Process] = None
        self._trigger_rerun = False
        self._last_connected: Optional[bool] = None

    # -- snapshot -----------------------------------------------------------------

    def _client_state(self) -> dict[str, Any]:
        st = self.client.state
        return {
            "plan": st.plan,
            "limits": st.limits,
            "connected_since_utc": format_utc(st.connected_since),
            "last_message_at_utc": format_utc(st.last_message_at),
            "reconnects": st.reconnects,
            "messages": st.messages,
            "subscribed": sorted(st.subscribed),
            "wanted_channels": self.client.wanted_channels(),
            "failed_channels": dict(st.failed_channels),
            "last_error": st.last_error,
            "live_observations": self.live_observations,
            "triggers_run": self.triggers_run,
            "station_days_tracked": len(self.days),
            "recent_latencies": list(st.recent_latencies),
            "platform_latency_ms": st.platform_latency_ms(),
            "delivery_latency_ms": st.delivery_latency_ms(),
            "forecast_records": len(self.forecasts),
            "forecasts_received": self.client.forecasts_received,
        }

    def _restore_days(self) -> None:
        snap = read_snapshot(self.snapshot_file)
        if snap is None:
            return
        n = self.days.restore(snap.get("days"))
        if n:
            LOG.info("restored %s station-day state(s) from %s", n, self.snapshot_file)
        nf = self.forecasts.restore(snap.get("forecasts"))
        if nf:
            LOG.info("restored %s forecast record(s)", nf)
        # seed the latest-observation cache too, so a restart is not blind until the first frame
        for icao, row in snap.get("stations", {}).items():
            obs = MetarObservation.from_dict(row) if isinstance(row, dict) else None
            if obs is not None:
                self.client.latest.setdefault(str(icao).upper(), obs)

    def _write_snapshot(self) -> None:
        # "connected" is recorded once the ack (plan, limits) is in; the initial disconnected state is not an event
        connected = self.client.state.connected and bool(self.client.state.plan)
        if connected != self._last_connected and not (self._last_connected is None and not connected):
            append_jsonl(self.events_file, {"kind": "connected" if connected else "disconnected",
                                       "plan": self.client.state.plan, "error": self.client.state.last_error})
        self._last_connected = connected
        try:
            payload = build_snapshot(
                self.client.latest,
                connected=self.client.state.connected,
                state=self._client_state(),
                days=self.days.to_dict(),
                forecasts=self.forecasts.to_dict(),
            )
            write_snapshot_atomic(self.snapshot_file, payload)
        except OSError as e:
            LOG.error("snapshot write failed (%s): %s", self.snapshot_file, e)

    # -- observations -------------------------------------------------------------

    async def _on_forecast(self, rec: ForecastRecord, cached: bool) -> None:
        if self.forecasts.ingest(rec):
            self._write_snapshot()
            if not cached:
                append_jsonl(self.events_file, {"kind": "forecast", "icao": rec.station, "model": rec.model, "local_date": rec.local_date,
                                               "temp_max_c": rec.temp_max_c, "temp_min_c": rec.temp_min_c, "run_utc": rec.run_utc.strftime("%Y-%m-%dT%H:%M:%SZ")})

    async def _on_observation(self, obs: MetarObservation, cached: bool) -> None:
        st = self.days.ingest(obs)
        LOG.debug("%s %s day max=%s min=%s n=%s", obs.icao_id, st.local_date, st.max_c, st.min_c, st.obs_count)
        self._write_snapshot()
        if cached:
            return
        self.live_observations += 1
        append_jsonl(self.events_file, {"kind": "observation", "icao": obs.icao_id, "temp_c": obs.temp_celsius,
                                   "obs_time": obs.report_time_utc, "speci": obs.is_speci})
        if not self.trigger_cmd:
            return
        if self.trigger_speci_only and not obs.is_speci:
            return
        self._schedule_trigger(reason=f"{obs.icao_id} {obs.report_time_utc}")

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

    # -- station list ---------------------------------------------------------------

    def _refresh_stations(self) -> None:
        if self.mapping_path is not None:
            self._tz_by_icao.update(timezones_from_mapping(self.mapping_path))
        if self.fixed_stations is not None:
            wanted = set(self.fixed_stations)
        elif self.mapping_path is not None:
            wanted = stations_from_mapping(self.mapping_path)
        else:
            wanted = set()
        if wanted != self.client.stations:
            LOG.info("stations: %s", ", ".join(sorted(wanted)) or "(none)")
            known = served_stations()
            unknown = sorted(wanted - known) if known else []
            if unknown:
                LOG.warning(
                    "not in data/metar_ws_stations.json (check the market's resolution station): %s",
                    ", ".join(unknown),
                )
            self.client.set_stations(wanted)
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
                self.client.stop()
                return
            if now >= next_reload:
                next_reload = now + self.mapping_reload_sec
                self._refresh_stations()
            if now >= next_beat:
                next_beat = now + self.heartbeat_sec
                self.days.prune()
                self.forecasts.prune()
                self._write_snapshot()

    # -- main ---------------------------------------------------------------------

    async def run(self, *, max_seconds: Optional[float] = None) -> int:
        self._refresh_stations()
        self._write_snapshot()
        hk = asyncio.ensure_future(self._housekeeping(max_seconds))
        try:
            await self.client.run()
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
        daemon.client.stop()

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
    daemon = Daemon(
        mapping_path=mapping_path_for_pipeline(pipeline),
        fixed_stations=fixed,
        trigger_cmd=(os.getenv("METAR_WS_TRIGGER_CMD") or "") if not args.no_trigger else "",
        trigger_debounce_sec=_env_float("METAR_WS_TRIGGER_DEBOUNCE_SEC", 5.0),
        trigger_speci_only=(os.getenv("METAR_WS_TRIGGER_ON_SPECI_ONLY") or "0").strip() == "1",
        mapping_reload_sec=_env_float("METAR_WS_MAPPING_RELOAD_SEC", 60.0),
        heartbeat_sec=_env_float("METAR_WS_SNAPSHOT_HEARTBEAT_SEC", 30.0),
    )
    _install_signal_handlers(daemon)
    LOG.info(
        "pipeline=%s snapshot=%s trigger=%r",
        pipeline,
        daemon.snapshot_file,
        daemon.trigger_cmd or None,
    )
    return await daemon.run(max_seconds=args.max_seconds)


def main() -> int:
    p = argparse.ArgumentParser(description="METAR.ws push daemon (snapshot + cycle trigger)")
    p.add_argument("--stations", default="", help="comma-separated ICAO list (overrides the mapping)")
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
