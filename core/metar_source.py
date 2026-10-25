"""
Observation source selection for the real-time pipelines.

``METAR_SOURCE``:

- ``awc``      (default) historical behaviour: poll aviationweather.gov on each cycle;
- ``metar_ws`` read the snapshot maintained by ``bots/metar_ws/metar_ws_daemon.py``
               (push delivery from ``wss://stream.metar.ws/v1/ws``). Set
               ``METAR_WS_FALLBACK_AWC=1`` to keep AWC as a safety net.

Both bot1 and bot2 registries are returned behind the same call so the
producers only change one line::

    registry = get_metar_registry("bot1")   # .fetch_latest_for_stations(icaos)
    registry = get_metar_registry("bot2")   # .fetch_latest_metar(icao)
"""
from __future__ import annotations

import logging
import os
from typing import Any

LOG = logging.getLogger("metar_source")

SOURCE_AWC = "awc"
SOURCE_METAR_WS = "metar_ws"
_ALIASES = {
    "awc": SOURCE_AWC,
    "noaa": SOURCE_AWC,
    "poll": SOURCE_AWC,
    "polling": SOURCE_AWC,
    "metar_ws": SOURCE_METAR_WS,
    "metarws": SOURCE_METAR_WS,
    "metar.ws": SOURCE_METAR_WS,
    "ws": SOURCE_METAR_WS,
    "push": SOURCE_METAR_WS,
}


def selected_source() -> str:
    raw = (os.getenv("METAR_SOURCE") or SOURCE_AWC).strip().lower()
    src = _ALIASES.get(raw)
    if src is None:
        LOG.warning("unknown METAR_SOURCE=%r, falling back to %s", raw, SOURCE_AWC)
        return SOURCE_AWC
    return src


class AwcRegistry:
    """
    One AWC polling registry for both pipelines: ``fetch_latest_for_stations`` (bot1 shape)
    from the bot1 implementation, plus the bot2 ``fetch_latest_metar`` dict shape on top.
    """

    def __init__(self) -> None:
        from bots.bot1_realtime.metar_live.realtime_metar_registry import RealTimeMetarRegistry

        self._inner = RealTimeMetarRegistry()

    def fetch_latest_for_stations(self, icao_list: Any) -> dict[str, Any]:
        return self._inner.fetch_latest_for_stations(icao_list)

    def fetch_latest_metar(self, icao_code: str) -> Any:
        code = (icao_code or "").strip().upper()
        if not code:
            return None
        try:
            obs = self._inner.fetch_latest_for_stations([code]).get(code)
        except Exception as e:  # noqa: BLE001
            LOG.error("AWC fetch failed for %s: %s", code, e)
            return None
        return obs.to_bot2_dict() if obs is not None else None


def _awc_registry(pipeline: str) -> Any:
    return AwcRegistry()


def get_metar_registry(pipeline: str = "bot2") -> Any:
    """Return the observation registry for ``pipeline`` (``"bot1"`` or ``"bot2"``)."""
    pipeline = (pipeline or "bot2").strip().lower()
    if pipeline not in ("bot1", "bot2"):
        raise ValueError(f"unknown pipeline {pipeline!r} (expected bot1 or bot2)")
    src = selected_source()
    if src == SOURCE_AWC:
        return _awc_registry(pipeline)

    from core.metar_ws_snapshot import MetarWsSnapshotRegistry

    fallback = None
    if (os.getenv("METAR_WS_FALLBACK_AWC") or "0").strip() == "1":
        fallback = _awc_registry(pipeline)
    LOG.info("observation source: METAR.ws snapshot%s", " + AWC fallback" if fallback else "")
    return MetarWsSnapshotRegistry(fallback=fallback)
