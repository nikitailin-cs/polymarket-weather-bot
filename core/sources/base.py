"""Source / Sink contract shared by every data source."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Optional

from core.forecast_state import ForecastRecord
from core.metar_observation import MetarObservation

ObsCallback = Callable[[MetarObservation, bool, str], Optional[Awaitable[None]]]
ForecastCallback = Callable[[ForecastRecord, bool, str], Optional[Awaitable[None]]]
StateCallback = Callable[[str], Optional[Awaitable[None]]]


@dataclass
class Sink:
    """Where sources deliver data. Callbacks receive ``(record, cached, source_name)``."""

    on_observation: Optional[ObsCallback] = None
    on_forecast: Optional[ForecastCallback] = None
    on_state_change: Optional[StateCallback] = None

    async def observation(self, obs: MetarObservation, cached: bool, source: str) -> None:
        if self.on_observation is not None:
            res = self.on_observation(obs, cached, source)
            if asyncio.iscoroutine(res):
                await res

    async def forecast(self, rec: ForecastRecord, cached: bool, source: str) -> None:
        if self.on_forecast is not None:
            res = self.on_forecast(rec, cached, source)
            if asyncio.iscoroutine(res):
                await res

    async def state_changed(self, source: str) -> None:
        if self.on_state_change is not None:
            res = self.on_state_change(source)
            if asyncio.iscoroutine(res):
                await res


@dataclass
class SourceState:
    """Health summary every source exposes (shown in the snapshot and the dashboard)."""

    name: str
    kind: str  # push | poll
    connected: bool = False
    last_message_at: Optional[datetime] = None
    messages: int = 0
    errors: int = 0
    last_error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "connected": self.connected,
            "last_message_at_utc": self.last_message_at.strftime("%Y-%m-%dT%H:%M:%SZ") if self.last_message_at else "",
            "messages": self.messages,
            "errors": self.errors,
            "last_error": self.last_error,
            **self.details,
        }


class Source(ABC):
    """A long-running producer of observations and/or forecasts."""

    name: str = "source"
    kind: str = "push"

    def __init__(self) -> None:
        self._stations: set[str] = set()
        self._stop = asyncio.Event()

    @property
    def stations(self) -> set[str]:
        return set(self._stations)

    def set_stations(self, icaos: Iterable[str]) -> None:
        """Update the station list; implementations re-subscribe / re-poll as needed."""
        self._stations = {str(x).strip().upper() for x in icaos if str(x).strip()}

    def stop(self) -> None:
        self._stop.set()

    @abstractmethod
    async def run(self, sink: Sink) -> None:
        """Run until :meth:`stop`; deliver everything through ``sink``."""

    @abstractmethod
    def state(self) -> SourceState:
        """Current health."""

    def latest_observations(self) -> dict[str, MetarObservation]:
        """Newest routine observation per station, when the source keeps one."""
        return {}

    def latest_fast_observations(self) -> dict[str, MetarObservation]:
        """Newest non-routine (frequent) observation per station, when the source keeps one."""
        return {}
