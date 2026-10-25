"""
Pluggable data sources for the real-time daemon.

A source is anything that produces :class:`core.metar_observation.MetarObservation`
and/or :class:`core.forecast_state.ForecastRecord` objects and hands them to a
:class:`Sink`. The daemon hosts the sources named in ``SOURCES`` (comma separated,
default ``metar_ws``) and keeps the station-day and forecast trackers, the snapshot
and the event log independent of where the data came from.

Built-in sources:

- ``metar_ws``   push stream wss://stream.metar.ws (routine METAR/SPECI, obs10 fast
                 observations, model forecasts)
- ``awc_poll``   polling of aviationweather.gov (routine METAR/SPECI only)

Adding one: subclass :class:`Source`, register it in :data:`REGISTRY`.
"""
from __future__ import annotations

from core.sources.base import Sink, Source, SourceState
from core.sources.registry import REGISTRY, build_sources, source_names_from_env

__all__ = ["REGISTRY", "Sink", "Source", "SourceState", "build_sources", "source_names_from_env"]
