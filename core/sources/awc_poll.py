"""
aviationweather.gov polling as a :class:`Source` (routine METAR/SPECI only).

Uses the same request logic as the historical bot1 registry
(``bots/bot1_realtime/metar_live/realtime_metar_registry.py``) but runs
continuously: every ``AWC_POLL_SEC`` (default 120) seconds it fetches the last
``AWC_POLL_HOURS`` (default 2) hours of reports for the station list and delivers
every report it has not delivered before. Useful as a fallback when the push
stream is unavailable, or to run the bot without a METAR.ws key.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from core.metar_observation import MetarObservation
from core.sources.base import Sink, Source, SourceState

LOG = logging.getLogger("source.awc_poll")


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


class AwcPollSource(Source):
    name = "awc_poll"
    kind = "poll"

    def __init__(self, *, interval_sec: Optional[float] = None, fetcher: Any = None) -> None:
        super().__init__()
        self.interval = interval_sec if interval_sec is not None else _env_float("AWC_POLL_SEC", 120.0)
        self._fetcher = fetcher  # callable(icaos) -> list[dict] (AWC JSON rows); default = HTTP registry
        self._state = SourceState(name=self.name, kind=self.kind)
        self._latest: dict[str, MetarObservation] = {}
        self._seen: set[tuple[str, str, str]] = set()
        self._wake = asyncio.Event()

    def set_stations(self, icaos) -> None:  # type: ignore[override]
        super().set_stations(icaos)
        self._wake.set()

    def stop(self) -> None:
        super().stop()
        self._wake.set()

    # -- fetching -----------------------------------------------------------------

    def _fetch_rows(self, icaos: list[str]) -> list[dict[str, Any]]:
        if self._fetcher is not None:
            return list(self._fetcher(icaos))
        from bots.bot1_realtime.metar_live.realtime_metar_registry import AWC_METAR_BASE
        import httpx

        rows: list[dict[str, Any]] = []
        hours = int(_env_float("AWC_POLL_HOURS", 2))
        for i in range(0, len(icaos), 15):
            chunk = icaos[i : i + 15]
            r = httpx.get(AWC_METAR_BASE, params={"ids": ",".join(chunk), "format": "json", "hours": str(hours)}, timeout=15.0,
                          headers={"User-Agent": "polymarket-weather-bot/awc_poll"})
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                rows.extend(x for x in data if isinstance(x, dict))
        return rows

    @staticmethod
    def observation_from_row(row: dict[str, Any]) -> Optional[MetarObservation]:
        from core.metar_observation import is_speci_raw, parse_utc

        icao = str(row.get("icaoId") or "").strip().upper()
        temp = row.get("temp")
        if not icao or temp is None:
            return None
        obs_utc = parse_utc(row.get("obsTime")) or parse_utc(row.get("reportTime"))
        if obs_utc is None:
            return None
        raw = str(row.get("rawOb") or "")
        speci = is_speci_raw(raw) or str(row.get("metarType") or "").upper() == "SPECI"
        dew = row.get("dewp")
        return MetarObservation(
            icao_id=icao,
            temp_celsius=float(temp),
            report_time_utc=str(row.get("reportTime") or obs_utc.strftime("%Y-%m-%dT%H:%M:%SZ")),
            raw_text=raw,
            observation_utc=obs_utc,
            is_speci=speci,
            dewpoint_celsius=float(dew) if dew is not None else None,
            received_at_utc=parse_utc(row.get("receiptTime")),
            source="awc",
            report_type="SPECI" if speci else "METAR",
            kind="speci" if speci else "metar",
        )

    # -- loop -----------------------------------------------------------------------

    async def run(self, sink: Sink) -> None:
        first = True
        while not self._stop.is_set():
            icaos = sorted(self._stations)
            if icaos:
                try:
                    rows = await asyncio.to_thread(self._fetch_rows, icaos)
                    n_new = 0
                    for row in rows:
                        obs = self.observation_from_row(row)
                        if obs is None:
                            continue
                        key = obs.dedupe_key()
                        if key in self._seen:
                            continue
                        self._seen.add(key)
                        if len(self._seen) > 50_000:
                            self._seen = set(list(self._seen)[-20_000:])
                        cur = self._latest.get(obs.icao_id)
                        if obs.kind == "metar" and (cur is None or obs.observation_utc >= cur.observation_utc):
                            self._latest[obs.icao_id] = obs
                        n_new += 1
                        self._state.messages += 1
                        await sink.observation(obs, first, self.name)
                    self._state.connected = True
                    self._state.last_error = ""
                    self._state.last_message_at = datetime.now(timezone.utc)
                    self._state.details.update({"last_poll_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                "stations": len(icaos), "new_reports": n_new, "interval_sec": self.interval})
                    LOG.info("awc poll: %s station(s), %s new report(s)", len(icaos), n_new)
                except Exception as e:  # noqa: BLE001
                    self._state.connected = False
                    self._state.errors += 1
                    self._state.last_error = f"{type(e).__name__}: {e}"
                    LOG.warning("awc poll failed: %s", self._state.last_error)
                await sink.state_changed(self.name)
                first = False
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def state(self) -> SourceState:
        return self._state

    def latest_observations(self) -> dict[str, MetarObservation]:
        return dict(self._latest)
