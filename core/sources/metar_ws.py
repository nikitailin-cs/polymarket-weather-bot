"""METAR.ws push stream as a :class:`Source` (routine METAR/SPECI, obs10, model forecasts)."""
from __future__ import annotations

from typing import Iterable, Optional

from core.metar_observation import MetarObservation
from core.metar_ws_client import MetarWsClient, MetarWsConfig
from core.sources.base import Sink, Source, SourceState


class MetarWsSource(Source):
    name = "metar_ws"
    kind = "push"

    def __init__(self, config: Optional[MetarWsConfig] = None, *, fast_stations: Optional[Iterable[str]] = None) -> None:
        super().__init__()
        self.config = config or MetarWsConfig.from_env()
        self.client = MetarWsClient(self.config)
        if fast_stations is not None:
            self.client.set_fast_stations(fast_stations)
        self._sink: Optional[Sink] = None

    def set_stations(self, icaos: Iterable[str]) -> None:
        super().set_stations(icaos)
        self.client.set_stations(self._stations)

    def set_fast_stations(self, icaos: Iterable[str]) -> None:
        self.client.set_fast_stations(icaos)

    def stop(self) -> None:
        super().stop()
        self.client.stop()

    async def run(self, sink: Sink) -> None:
        self._sink = sink
        self.client.on_observation = lambda obs, cached: sink.observation(obs, cached, self.name)
        self.client.on_forecast = lambda rec, cached: sink.forecast(rec, cached, self.name)
        self.client.on_state_change = lambda: sink.state_changed(self.name)
        await self.client.run()

    def state(self) -> SourceState:
        st = self.client.state
        return SourceState(
            name=self.name,
            kind=self.kind,
            connected=st.connected,
            last_message_at=st.last_message_at,
            messages=st.messages,
            errors=len(st.failed_channels),
            last_error=st.last_error,
            details={
                "plan": st.plan,
                "limits": st.limits,
                "connected_since_utc": st.connected_since.strftime("%Y-%m-%dT%H:%M:%SZ") if st.connected_since else "",
                "reconnects": st.reconnects,
                "subscribed": sorted(st.subscribed),
                "wanted_channels": self.client.wanted_channels(),
                "failed_channels": dict(st.failed_channels),
                "forecasts_received": self.client.forecasts_received,
                "fast_observations": self.client.fast_received,
                "recent_latencies": list(st.recent_latencies),
                "platform_latency_ms": st.platform_latency_ms(),
                "delivery_latency_ms": st.delivery_latency_ms(),
            },
        )

    def latest_observations(self) -> dict[str, MetarObservation]:
        return dict(self.client.latest)

    def latest_fast_observations(self) -> dict[str, MetarObservation]:
        return dict(self.client.latest_fast)
