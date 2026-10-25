"""
METAR.ws push client: replaces polling of the AWC HTTP API with a persistent
WebSocket subscription to ``wss://stream.metar.ws/v1/ws``.

Wire protocol (see METAR.ws ``docs/STREAM-PROTOCOL.md``):

- connect to ``<METAR_WS_URL>?key=<API_KEY>``; the server answers with ``ack``;
- send ``{"action": "subscribe", "channels": [...]}`` -> ``subscribed`` + the
  last cached value per channel (``cached: true``), then live ``publication`` frames;
- ``error`` frames are non-fatal (``channel_limit``, ``permission_denied``,
  ``unknown_station``, ...); the connection stays open.

Two publication payload schemas are accepted:

- station schema ``metar_observation.v1`` (channel ``metar.obs.icao.<icao>``):
  ``icao``, ``temperature_c`` (number), ``observation_time``, ``raw``,
  ``dewpoint_c``, ``source_received_at``;
- legacy city schema (channel ``metar.obs.<city>``): ``location``, ``temp_c``
  (decimal as string), ``report_time``, ``raw``. ICAO is read from the raw text.

Environment:

- ``METAR_WS_API_KEY``            API key (``mts_live_...``). Required.
- ``METAR_WS_URL``                default ``wss://stream.metar.ws/v1/ws``
- ``METAR_WS_CHANNEL_FORMAT``     default ``metar.obs.icao.{icao}`` (``{icao}`` lower-case)
- ``METAR_WS_CHANNEL_MAP``        optional JSON ``{"EGLL": "metar.obs.london"}`` overriding
                                  the format for specific stations (legacy city channels)
- ``METAR_WS_CHANNEL_MAP_FILE``   same, from a JSON file
- ``METAR_WS_RECONNECT_MIN_SEC`` / ``METAR_WS_RECONNECT_MAX_SEC``  backoff bounds (1 / 60)
- ``METAR_WS_PING_INTERVAL_SEC``  WebSocket keep-alive ping (20)
- ``METAR_WS_CA_BUNDLE``          PEM bundle for TLS verification (default: certifi, else system)
- ``METAR_WS_SSL_VERIFY``         set to ``0`` to skip TLS verification (debug only)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional
from urllib.parse import urlencode

from core.metar_observation import (
    MetarObservation,
    icao_from_raw,
    is_speci_raw,
    parse_utc,
)

LOG = logging.getLogger("metar_ws")

DEFAULT_URL = "wss://stream.metar.ws/v1/ws"
DEFAULT_CHANNEL_FORMAT = "metar.obs.icao.{icao}"
SOURCE_NAME = "metar_ws"

ObservationCallback = Callable[[MetarObservation, bool], Optional[Awaitable[None]]]
StateCallback = Callable[[], Optional[Awaitable[None]]]


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _load_channel_map() -> dict[str, str]:
    """Explicit ICAO -> channel overrides from env JSON and/or a JSON file."""
    out: dict[str, str] = {}
    file_raw = (os.getenv("METAR_WS_CHANNEL_MAP_FILE") or "").strip()
    if file_raw:
        try:
            data = json.loads(Path(file_raw).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out.update({str(k).upper(): str(v) for k, v in data.items()})
        except (OSError, ValueError) as e:
            LOG.warning("METAR_WS_CHANNEL_MAP_FILE unreadable (%s): %s", file_raw, e)
    env_raw = (os.getenv("METAR_WS_CHANNEL_MAP") or "").strip()
    if env_raw:
        try:
            data = json.loads(env_raw)
            if isinstance(data, dict):
                out.update({str(k).upper(): str(v) for k, v in data.items()})
        except ValueError as e:
            LOG.warning("METAR_WS_CHANNEL_MAP is not valid JSON: %s", e)
    return out


@dataclass
class MetarWsConfig:
    api_key: str = ""
    url: str = DEFAULT_URL
    channel_format: str = DEFAULT_CHANNEL_FORMAT
    channel_map: dict[str, str] = field(default_factory=dict)
    reconnect_min_sec: float = 1.0
    reconnect_max_sec: float = 60.0
    ping_interval_sec: float = 20.0
    ssl_verify: bool = True
    ca_bundle: str = ""

    @classmethod
    def from_env(cls) -> "MetarWsConfig":
        return cls(
            ca_bundle=(os.getenv("METAR_WS_CA_BUNDLE") or "").strip(),
            api_key=(os.getenv("METAR_WS_API_KEY") or "").strip(),
            url=(os.getenv("METAR_WS_URL") or DEFAULT_URL).strip() or DEFAULT_URL,
            channel_format=(os.getenv("METAR_WS_CHANNEL_FORMAT") or DEFAULT_CHANNEL_FORMAT).strip()
            or DEFAULT_CHANNEL_FORMAT,
            channel_map=_load_channel_map(),
            reconnect_min_sec=max(0.1, _env_float("METAR_WS_RECONNECT_MIN_SEC", 1.0)),
            reconnect_max_sec=max(1.0, _env_float("METAR_WS_RECONNECT_MAX_SEC", 60.0)),
            ping_interval_sec=max(5.0, _env_float("METAR_WS_PING_INTERVAL_SEC", 20.0)),
            ssl_verify=(os.getenv("METAR_WS_SSL_VERIFY", "1").strip() != "0"),
        )

    def channel_for(self, icao: str) -> str:
        code = (icao or "").strip().upper()
        if code in self.channel_map:
            return self.channel_map[code]
        return self.channel_format.format(icao=code.lower(), ICAO=code)

    def connect_url(self) -> str:
        sep = "&" if "?" in self.url else "?"
        return f"{self.url}{sep}{urlencode({'key': self.api_key})}"


def build_ssl_context(config: "MetarWsConfig") -> Any:
    """
    TLS context for ``wss://``. Verification stays on by default. The CA bundle is,
    in order: ``METAR_WS_CA_BUNDLE`` (``config.ca_bundle``), the ``certifi`` bundle
    when installed, else the interpreter's default store. The Let's Encrypt chain
    used by stream.metar.ws (Root YE / ISRG Root X2) is rejected as "expired" by
    some Windows system stores while certifi validates it, hence the preference.
    ``METAR_WS_SSL_VERIFY=0`` disables verification (debugging only, logged loudly).
    """
    import ssl

    if not config.ssl_verify:
        LOG.warning("TLS verification DISABLED (METAR_WS_SSL_VERIFY=0) - debugging only")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    cafile = config.ca_bundle or None
    if not cafile:
        try:
            import certifi

            cafile = certifi.where()
        except ImportError:
            cafile = None
    if cafile:
        LOG.debug("TLS CA bundle: %s", cafile)
    return ssl.create_default_context(cafile=cafile)


def icao_from_channel(channel: str) -> str:
    """``metar.obs.icao.knyc`` -> ``KNYC``; city channels yield ``""``."""
    parts = (channel or "").strip().split(".")
    if len(parts) >= 4 and parts[0] == "metar" and parts[1] == "obs" and parts[2] == "icao":
        code = parts[3].strip().upper()
        if len(code) == 4:
            return code
    return ""


def normalize_publication(
    msg: dict[str, Any],
    channel_to_icao: Optional[dict[str, str]] = None,
) -> Optional[MetarObservation]:
    """
    Convert a ``publication`` frame into a :class:`MetarObservation`.
    Returns ``None`` when the frame carries no usable temperature observation.
    """
    if not isinstance(msg, dict) or msg.get("type") != "publication":
        return None
    data = msg.get("data")
    if not isinstance(data, dict):
        return None
    channel = str(msg.get("channel") or "")

    raw = str(data.get("raw") or data.get("rawOb") or "").strip()

    # station identity: payload field, then ICAO channel, then the raw report text
    # (authoritative for city channels), and only then the configured channel map
    icao = str(data.get("icao") or data.get("icaoId") or "").strip().upper()
    if not icao:
        icao = icao_from_channel(channel)
    if not icao:
        icao = icao_from_raw(raw)
    if not icao and channel_to_icao:
        icao = channel_to_icao.get(channel, "")
    if not icao:
        LOG.debug("publication without station identity on %s ignored", channel)
        return None

    temp_raw = data.get("temperature_c")
    if temp_raw is None:
        temp_raw = data.get("temp_c")
    if temp_raw is None:
        temp_raw = data.get("temp")
    try:
        temp_c = float(str(temp_raw).strip())
    except (TypeError, ValueError):
        LOG.debug("publication for %s without temperature ignored", icao)
        return None

    obs_utc = parse_utc(
        data.get("observation_time") or data.get("report_time") or data.get("obsTime")
    )
    if obs_utc is None:
        obs_utc = parse_utc(msg.get("published_at")) or datetime.now(timezone.utc)

    dew_raw = data.get("dewpoint_c")
    if dew_raw is None:
        dew_raw = data.get("dewp")
    try:
        dew_c = float(str(dew_raw).strip()) if dew_raw is not None else None
    except (TypeError, ValueError):
        dew_c = None

    received = parse_utc(data.get("source_received_at") or data.get("receiptTime")) or parse_utc(
        msg.get("published_at")
    )

    return MetarObservation(
        icao_id=icao,
        temp_celsius=temp_c,
        report_time_utc=str(data.get("observation_time") or data.get("report_time") or ""),
        raw_text=raw,
        observation_utc=obs_utc,
        is_speci=is_speci_raw(raw),
        dewpoint_celsius=dew_c,
        received_at_utc=received,
        source=SOURCE_NAME,
    )


def is_newer(candidate: MetarObservation, current: Optional[MetarObservation]) -> bool:
    """Newer observation time wins; on equal time a SPECI beats a routine METAR."""
    if current is None:
        return True
    if candidate.observation_utc != current.observation_utc:
        return candidate.observation_utc > current.observation_utc
    return candidate.is_speci and not current.is_speci


@dataclass
class ClientState:
    connected: bool = False
    connected_since: Optional[datetime] = None
    last_message_at: Optional[datetime] = None
    plan: str = ""
    limits: dict[str, Any] = field(default_factory=dict)
    reconnects: int = 0
    messages: int = 0
    subscribed: set[str] = field(default_factory=set)
    failed_channels: dict[str, str] = field(default_factory=dict)
    last_error: str = ""


class MetarWsClient:
    """
    Long-lived subscriber. Call :meth:`set_stations` with the ICAO codes you need,
    then run :meth:`run` as an asyncio task. Observations are kept in :attr:`latest`
    (one per station, newest wins) and pushed to ``on_observation(obs, cached)``.
    """

    def __init__(
        self,
        config: Optional[MetarWsConfig] = None,
        *,
        on_observation: Optional[ObservationCallback] = None,
        on_state_change: Optional[StateCallback] = None,
    ) -> None:
        self.config = config or MetarWsConfig.from_env()
        self.on_observation = on_observation
        self.on_state_change = on_state_change
        self.latest: dict[str, MetarObservation] = {}
        self.state = ClientState()
        self._stations: set[str] = set()
        self._channel_to_icao: dict[str, str] = {}
        self._seen: set[tuple[str, str, str]] = set()
        self._stop = asyncio.Event()
        self._resubscribe = asyncio.Event()

    # -- public API -----------------------------------------------------------

    @property
    def stations(self) -> set[str]:
        return set(self._stations)

    def set_stations(self, icaos: Iterable[str]) -> None:
        wanted = {str(x).strip().upper() for x in icaos if str(x).strip()}
        if wanted == self._stations:
            return
        self._stations = wanted
        self._channel_to_icao = {self.config.channel_for(i): i for i in wanted}
        self._resubscribe.set()

    def wanted_channels(self) -> list[str]:
        return sorted(self._channel_to_icao.keys())

    def stop(self) -> None:
        self._stop.set()
        self._resubscribe.set()

    async def run(self) -> None:
        """Connect, subscribe, read until :meth:`stop`; reconnect with backoff on failure."""
        if not self.config.api_key:
            raise RuntimeError("METAR_WS_API_KEY is empty: cannot connect to METAR.ws")
        import websockets  # lazy: unit tests without the dependency can still import this module

        backoff = self.config.reconnect_min_sec
        while not self._stop.is_set():
            try:
                kwargs: dict[str, Any] = {
                    "ping_interval": self.config.ping_interval_sec,
                    "ping_timeout": self.config.ping_interval_sec,
                    "close_timeout": 5,
                    "max_size": 2**20,
                }
                if self.config.url.startswith("wss://"):
                    kwargs["ssl"] = build_ssl_context(self.config)
                LOG.info("connecting to %s", self.config.url)
                async with websockets.connect(self.config.connect_url(), **kwargs) as ws:
                    await self._on_connected()
                    backoff = self.config.reconnect_min_sec
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any transport failure leads to a reconnect
                self.state.last_error = f"{type(e).__name__}: {e}"
                LOG.warning("connection lost (%s)", self.state.last_error)
            finally:
                if self.state.connected:
                    self.state.connected = False
                    self.state.subscribed.clear()
                    await self._notify_state()
            if self._stop.is_set():
                break
            sleep_s = min(backoff, self.config.reconnect_max_sec) * random.uniform(0.8, 1.2)
            LOG.info("reconnecting in %.1fs", sleep_s)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.config.reconnect_max_sec)
            self.state.reconnects += 1

    # -- internals ------------------------------------------------------------

    async def _on_connected(self) -> None:
        self.state.connected = True
        self.state.connected_since = datetime.now(timezone.utc)
        self.state.last_error = ""
        self.state.subscribed.clear()
        self._resubscribe.set()
        await self._notify_state()

    async def _session(self, ws: Any) -> None:
        reader = asyncio.ensure_future(self._reader(ws))
        try:
            while not self._stop.is_set() and not reader.done():
                await self._resubscribe.wait()
                self._resubscribe.clear()
                if self._stop.is_set() or reader.done():
                    break
                await self._sync_subscriptions(ws)
                waiter = asyncio.ensure_future(self._resubscribe.wait())
                done, _ = await asyncio.wait({reader, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if waiter not in done:
                    waiter.cancel()
                    try:
                        await waiter
                    except asyncio.CancelledError:
                        pass
        finally:
            if not reader.done():
                reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if not self._stop.is_set():
            exc = reader.exception() if reader.done() and not reader.cancelled() else None
            if exc:
                raise exc
            raise ConnectionError("stream closed")

    async def _sync_subscriptions(self, ws: Any) -> None:
        wanted = set(self.wanted_channels())
        to_add = sorted(wanted - self.state.subscribed)
        to_remove = sorted(self.state.subscribed - wanted)
        if to_remove:
            await ws.send(json.dumps({"action": "unsubscribe", "channels": to_remove}))
            LOG.info("unsubscribe %s", ", ".join(to_remove))
        if to_add:
            await ws.send(json.dumps({"action": "subscribe", "channels": to_add}))
            LOG.info("subscribe %s", ", ".join(to_add))

    async def _reader(self, ws: Any) -> None:
        async for frame in ws:
            self.state.messages += 1
            self.state.last_message_at = datetime.now(timezone.utc)
            try:
                msg = json.loads(frame)
            except ValueError:
                LOG.warning("non-JSON frame ignored")
                continue
            if not isinstance(msg, dict):
                continue
            await self.handle_message(msg)

    async def handle_message(self, msg: dict[str, Any]) -> None:
        """Dispatch one decoded server frame (public for tests)."""
        mtype = msg.get("type")
        if mtype == "publication":
            obs = normalize_publication(msg, self._channel_to_icao)
            if obs is None:
                return
            cached = bool(msg.get("cached", False))
            key = obs.dedupe_key()
            if key in self._seen:
                LOG.debug("duplicate observation %s @ %s ignored", obs.icao_id, obs.report_time_utc)
                return
            self._seen.add(key)
            if len(self._seen) > 50_000:
                self._seen = set(list(self._seen)[-20_000:])
            if is_newer(obs, self.latest.get(obs.icao_id)):
                self.latest[obs.icao_id] = obs
            LOG.info(
                "%s %s %.1fC %s%s",
                "cached" if cached else "live",
                obs.icao_id,
                obs.temp_celsius,
                obs.report_time_utc,
                " SPECI" if obs.is_speci else "",
            )
            if self.on_observation is not None:
                res = self.on_observation(obs, cached)
                if asyncio.iscoroutine(res):
                    await res
        elif mtype == "ack":
            self.state.plan = str(msg.get("plan") or "")
            limits = msg.get("limits")
            self.state.limits = dict(limits) if isinstance(limits, dict) else {}
            LOG.info("ack: plan=%s limits=%s", self.state.plan, self.state.limits)
            await self._notify_state()
        elif mtype == "subscribed":
            chans = msg.get("channels") or []
            self.state.subscribed.update(str(c) for c in chans)
            for c in chans:
                self.state.failed_channels.pop(str(c), None)
            await self._notify_state()
        elif mtype == "unsubscribed":
            for c in msg.get("channels") or []:
                self.state.subscribed.discard(str(c))
            await self._notify_state()
        elif mtype == "error":
            code = str(msg.get("code") or "error")
            chan = msg.get("channel")
            LOG.error("server error %s: %s%s", code, msg.get("message"), f" ({chan})" if chan else "")
            self.state.last_error = f"{code}: {msg.get('message')}"
            if chan:
                self.state.failed_channels[str(chan)] = code
            await self._notify_state()
        else:
            LOG.debug("unknown frame type %r ignored", mtype)

    async def _notify_state(self) -> None:
        if self.on_state_change is None:
            return
        res = self.on_state_change()
        if asyncio.iscoroutine(res):
            await res
