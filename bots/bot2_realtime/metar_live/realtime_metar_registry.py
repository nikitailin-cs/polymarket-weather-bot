"""
Bot 2 METAR registry: alias of the shared AWC implementation (``core.metar_source.AwcRegistry``).

The former copy of the polling code lived here; both pipelines now use one implementation
(bot1's ``RealTimeMetarRegistry``) and this module keeps the historical import path and the
``fetch_latest_metar(icao) -> dict | None`` shape.
"""
from __future__ import annotations

from core.metar_source import AwcRegistry as RealTimeMetarRegistry  # noqa: F401

__all__ = ["RealTimeMetarRegistry"]
