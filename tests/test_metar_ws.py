"""
Tests for the METAR.ws push integration:

- publication normalisation (station schema and legacy city schema);
- client frame handling (dedupe, SPECI priority, errors, ack);
- snapshot round-trip and staleness rules of the registry bridge;
- source factory (``METAR_SOURCE``);
- end-to-end against a local fake METAR.ws server (connect, ack, subscribe,
  cached + live publication, reconnect and resubscribe after a server drop).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("DRY_RUN", "1")
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.metar_observation import MetarObservation, icao_from_raw, parse_utc  # noqa: E402
from core.metar_ws_client import (  # noqa: E402
    MetarWsClient,
    MetarWsConfig,
    icao_from_channel,
    is_newer,
    normalize_publication,
)
from core.metar_ws_snapshot import (  # noqa: E402
    MetarWsSnapshotRegistry,
    build_snapshot,
    read_snapshot,
    write_snapshot_atomic,
)

import importlib.util

HAVE_WEBSOCKETS = importlib.util.find_spec("websockets") is not None


STATION_FRAME = {
    "type": "publication",
    "channel": "metar.obs.icao.knyc",
    "cached": False,
    "published_at": "2026-10-25T14:51:40.100Z",
    "data": {
        "schema": "metar_observation.v1",
        "event": "observation_received",
        "icao": "KNYC",
        "observation_time": "2026-10-25T14:51:00Z",
        "source_received_at": "2026-10-25T14:51:39.802Z",
        "raw": "METAR KNYC 251451Z 27010KT 10SM FEW050 17/06 A3012",
        "temperature_c": 17,
        "dewpoint_c": 6,
    },
}

CITY_FRAME = {
    "type": "publication",
    "channel": "metar.obs.london",
    "data": {
        "location": "London",
        "temp_c": "12.3",
        "report_time": "2026-10-25T14:50:00Z",
        "raw": "METAR EGLL 251450Z 24008KT 9999 SCT030 12/08 Q1015",
    },
}


class TestNormalize(unittest.TestCase):
    def test_station_schema(self):
        obs = normalize_publication(STATION_FRAME)
        self.assertIsNotNone(obs)
        self.assertEqual(obs.icao_id, "KNYC")
        self.assertEqual(obs.temp_celsius, 17.0)
        self.assertEqual(obs.dewpoint_celsius, 6.0)
        self.assertEqual(obs.observation_utc, datetime(2026, 10, 25, 14, 51, tzinfo=timezone.utc))
        self.assertEqual(obs.received_at_utc, parse_utc("2026-10-25T14:51:39.802Z"))
        self.assertFalse(obs.is_speci)
        self.assertAlmostEqual(obs.temp_fahrenheit, 62.6, places=3)
        self.assertEqual(obs.source, "metar_ws")

    def test_city_schema_icao_from_raw(self):
        obs = normalize_publication(CITY_FRAME)
        self.assertEqual(obs.icao_id, "EGLL")
        self.assertEqual(obs.temp_celsius, 12.3)
        self.assertEqual(obs.report_time_utc, "2026-10-25T14:50:00Z")

    def test_city_schema_icao_from_channel_map(self):
        frame = json.loads(json.dumps(CITY_FRAME))
        frame["data"]["raw"] = ""
        self.assertIsNone(normalize_publication(frame))
        obs = normalize_publication(frame, {"metar.obs.london": "EGLL"})
        self.assertEqual(obs.icao_id, "EGLL")

    def test_icao_from_channel(self):
        self.assertEqual(icao_from_channel("metar.obs.icao.lzib"), "LZIB")
        self.assertEqual(icao_from_channel("metar.obs.klga"), "KLGA")
        self.assertEqual(icao_from_channel("metar.obs.london"), "")
        self.assertEqual(icao_from_channel("metar.obs.sao-paulo"), "")
        self.assertEqual(icao_from_channel("model.icon.temperature-extrema.icao.lzib"), "")

    def test_speci_flag_and_icao_from_raw(self):
        frame = json.loads(json.dumps(STATION_FRAME))
        frame["data"]["raw"] = "SPECI KNYC 251512Z 27015G25KT 10SM SCT050 19/06 A3010"
        frame["data"]["observation_time"] = "2026-10-25T15:12:00Z"
        obs = normalize_publication(frame)
        self.assertTrue(obs.is_speci)
        self.assertEqual(icao_from_raw(frame["data"]["raw"]), "KNYC")

    def test_rejects_non_publication_and_missing_temperature(self):
        self.assertIsNone(normalize_publication({"type": "ack", "plan": "starter"}))
        frame = json.loads(json.dumps(STATION_FRAME))
        del frame["data"]["temperature_c"]
        self.assertIsNone(normalize_publication(frame))
        frame["data"]["temperature_c"] = "n/a"
        self.assertIsNone(normalize_publication(frame))

    def test_is_newer_prefers_time_then_speci(self):
        a = normalize_publication(STATION_FRAME)
        later = json.loads(json.dumps(STATION_FRAME))
        later["data"]["observation_time"] = "2026-10-25T15:51:00Z"
        b = normalize_publication(later)
        self.assertTrue(is_newer(b, a))
        self.assertFalse(is_newer(a, b))
        speci = json.loads(json.dumps(STATION_FRAME))
        speci["data"]["raw"] = "SPECI KNYC 251451Z 27010KT 10SM FEW050 18/06 A3012"
        c = normalize_publication(speci)
        self.assertTrue(is_newer(c, a))
        self.assertFalse(is_newer(a, c))


class TestConfig(unittest.TestCase):
    def test_channel_format_and_map(self):
        cfg = MetarWsConfig(api_key="k", channel_map={"EGLL": "metar.obs.london"})
        self.assertEqual(cfg.channel_for("knyc"), "metar.obs.knyc")
        self.assertEqual(cfg.channel_for("EGLL"), "metar.obs.london")
        self.assertEqual(cfg.connect_url(), "wss://stream.metar.ws/v1/ws?key=k")

    def test_from_env_channel_map_json(self):
        with mock.patch.dict(os.environ, {"METAR_WS_API_KEY": "x", "METAR_WS_CHANNEL_MAP": '{"egll": "metar.obs.london"}'}):
            cfg = MetarWsConfig.from_env()
        self.assertEqual(cfg.channel_map, {"EGLL": "metar.obs.london"})
        self.assertEqual(cfg.api_key, "x")


class TestClientFrames(unittest.TestCase):
    def _client(self, seen: list):
        cfg = MetarWsConfig(api_key="k", forecasts=False)
        client = MetarWsClient(cfg, on_observation=lambda obs, cached: seen.append((obs.icao_id, cached)))
        client.set_stations(["KNYC", "EGLL"])
        return client

    def test_dedupe_and_latest(self):
        seen: list = []
        client = self._client(seen)

        async def go():
            cached = dict(STATION_FRAME, cached=True)
            await client.handle_message(cached)
            await client.handle_message(STATION_FRAME)  # same observation again -> ignored
            await client.handle_message(CITY_FRAME)

        asyncio.run(go())
        self.assertEqual(seen, [("KNYC", True), ("EGLL", False)])
        self.assertEqual(set(client.latest), {"KNYC", "EGLL"})

    def test_ack_subscribed_error_state(self):
        client = self._client([])

        async def go():
            await client.handle_message({"type": "ack", "plan": "starter", "limits": {"max_channels": 10}})
            await client.handle_message({"type": "subscribed", "channels": ["metar.obs.icao.knyc"]})
            await client.handle_message(
                {"type": "error", "code": "unknown_station", "message": "nope", "action": "subscribe",
                 "channel": "metar.obs.icao.egll"}
            )

        asyncio.run(go())
        self.assertEqual(client.state.plan, "starter")
        self.assertEqual(client.state.limits, {"max_channels": 10})
        self.assertIn("metar.obs.icao.knyc", client.state.subscribed)
        self.assertEqual(client.state.failed_channels, {"metar.obs.icao.egll": "unknown_station"})

    def test_wanted_channels_follow_stations(self):
        client = self._client([])
        self.assertEqual(client.wanted_channels(), ["metar.obs.egll", "metar.obs.knyc"])
        client.set_stations(["KORD"])
        self.assertEqual(client.wanted_channels(), ["metar.obs.kord"])


class TestSnapshotRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "snap" / "metar_ws_latest.json"
        self.now = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
        self.obs = normalize_publication(STATION_FRAME)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, generated_at: datetime, latest=None, connected=True):
        payload = build_snapshot(latest if latest is not None else {"KNYC": self.obs}, connected=connected, now=generated_at)
        write_snapshot_atomic(self.path, payload)
        return payload

    def test_roundtrip_bot1_and_bot2_shapes(self):
        self._write(self.now - timedelta(seconds=10))
        reg = MetarWsSnapshotRegistry(path=self.path, now_fn=lambda: self.now)
        by = reg.fetch_latest_for_stations(["knyc", "KORD"])
        self.assertEqual(set(by), {"KNYC"})
        o = by["KNYC"]
        self.assertIsInstance(o, MetarObservation)
        self.assertEqual(o.temp_celsius, 17.0)
        self.assertEqual(o.observation_utc, self.obs.observation_utc)
        d = reg.fetch_latest_metar("KNYC")
        self.assertEqual(d["temp_c"], 17.0)
        self.assertEqual(d["temp_f"], 62.6)
        self.assertEqual(d["obsTime"], "2026-10-25T14:51:00Z")
        self.assertTrue(d["rawOb"].startswith("METAR KNYC"))
        self.assertIsNone(reg.fetch_latest_metar("KORD"))
        self.assertEqual(reg.last_status, "ok")

    def test_stale_snapshot_returns_nothing(self):
        self._write(self.now - timedelta(seconds=600))
        reg = MetarWsSnapshotRegistry(path=self.path, max_age_sec=180, now_fn=lambda: self.now)
        self.assertEqual(reg.fetch_latest_for_stations(["KNYC"]), {})
        self.assertEqual(reg.last_status, "stale")

    def test_missing_snapshot(self):
        reg = MetarWsSnapshotRegistry(path=self.path, now_fn=lambda: self.now)
        self.assertEqual(reg.fetch_latest_for_stations(["KNYC"]), {})
        self.assertEqual(reg.last_status, "missing")
        self.assertIsNone(read_snapshot(self.path))

    def test_old_observation_dropped(self):
        self._write(self.now - timedelta(seconds=5))
        late = self.now + timedelta(hours=3)
        reg = MetarWsSnapshotRegistry(path=self.path, max_age_sec=10 ** 6, obs_max_age_sec=7200, now_fn=lambda: late)
        self.assertEqual(reg.fetch_latest_for_stations(["KNYC"]), {})

    def test_fallback_used_for_missing_and_stale(self):
        class FakeAwc:
            def __init__(self):
                self.calls = []

            def fetch_latest_for_stations(self, ids):
                self.calls.append(("bot1", list(ids)))
                return {"KORD": MetarObservation("KORD", 20.0, "x", "METAR KORD", datetime.now(timezone.utc), False)}

            def fetch_latest_metar(self, icao):
                self.calls.append(("bot2", icao))
                return {"icao": icao, "temp_c": 1.0, "temp_f": 33.8}

        self._write(self.now - timedelta(seconds=10))
        awc = FakeAwc()
        reg = MetarWsSnapshotRegistry(path=self.path, fallback=awc, now_fn=lambda: self.now)
        by = reg.fetch_latest_for_stations(["KNYC", "KORD"])
        self.assertEqual(set(by), {"KNYC", "KORD"})
        self.assertEqual(awc.calls, [("bot1", ["KORD"])])
        self.assertEqual(reg.fetch_latest_metar("KSEA")["temp_c"], 1.0)
        # stale snapshot -> everything through the fallback
        self._write(self.now - timedelta(hours=1))
        awc.calls.clear()
        reg.fetch_latest_for_stations(["KNYC"])
        self.assertEqual(awc.calls, [("bot1", ["KNYC"])])


class TestSourceFactory(unittest.TestCase):
    def test_default_is_awc(self):
        from core.metar_source import get_metar_registry, selected_source

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("METAR_SOURCE", None)
            self.assertEqual(selected_source(), "awc")
            reg = get_metar_registry("bot2")
            self.assertTrue(hasattr(reg, "fetch_latest_metar"))
            self.assertEqual(type(reg).__module__, "bots.bot2_realtime.metar_live.realtime_metar_registry")
            reg1 = get_metar_registry("bot1")
            self.assertEqual(type(reg1).__module__, "bots.bot1_realtime.metar_live.realtime_metar_registry")

    def test_metar_ws_with_and_without_fallback(self):
        from core.metar_source import get_metar_registry

        with mock.patch.dict(os.environ, {"METAR_SOURCE": "metar_ws", "METAR_WS_FALLBACK_AWC": "0"}):
            reg = get_metar_registry("bot1")
            self.assertIsInstance(reg, MetarWsSnapshotRegistry)
            self.assertIsNone(reg.fallback)
        with mock.patch.dict(os.environ, {"METAR_SOURCE": "push", "METAR_WS_FALLBACK_AWC": "1"}):
            reg = get_metar_registry("bot2")
            self.assertIsInstance(reg, MetarWsSnapshotRegistry)
            self.assertTrue(hasattr(reg.fallback, "fetch_latest_metar"))

    def test_unknown_pipeline(self):
        from core.metar_source import get_metar_registry

        with self.assertRaises(ValueError):
            get_metar_registry("bot9")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(HAVE_WEBSOCKETS, "websockets not installed")
class TestEndToEnd(unittest.TestCase):
    """Fake METAR.ws server: ack -> subscribe -> cached + live -> drop -> reconnect."""

    def test_connect_subscribe_reconnect(self):
        import websockets

        port = _free_port()
        server_log: list = []
        connections = {"n": 0}
        seen: list = []

        async def handler(ws, *_):
            connections["n"] += 1
            n = connections["n"]
            server_log.append(("connect", n, ws.path if hasattr(ws, "path") else ""))
            await ws.send(json.dumps({"type": "ack", "plan": "starter", "limits": {"max_channels": 10}}))
            async for frame in ws:
                msg = json.loads(frame)
                server_log.append((msg.get("action"), n, tuple(msg.get("channels", []))))
                if msg.get("action") == "subscribe":
                    await ws.send(json.dumps({"type": "subscribed", "channels": msg["channels"]}))
                    if n == 1:
                        await ws.send(json.dumps(dict(STATION_FRAME, cached=True)))
                        live = json.loads(json.dumps(STATION_FRAME))
                        live["data"]["observation_time"] = "2026-10-25T15:51:00Z"
                        live["data"]["temperature_c"] = 18
                        await ws.send(json.dumps(live))
                        await asyncio.sleep(0.05)
                        await ws.close()  # simulate a server-side drop
                        return
                    else:
                        live2 = json.loads(json.dumps(STATION_FRAME))
                        live2["data"]["observation_time"] = "2026-10-25T16:51:00Z"
                        live2["data"]["temperature_c"] = 19
                        await ws.send(json.dumps(live2))

        async def go():
            async with websockets.serve(handler, "127.0.0.1", port):
                cfg = MetarWsConfig(
                    api_key="test-key",
                    url=f"ws://127.0.0.1:{port}/v1/ws",
                    reconnect_min_sec=0.1,
                    reconnect_max_sec=0.2,
                    ping_interval_sec=5,
                    forecasts=False,
                )
                client = MetarWsClient(cfg, on_observation=lambda o, c: seen.append((o.icao_id, o.temp_celsius, c)))
                client.set_stations(["KNYC"])
                task = asyncio.ensure_future(client.run())
                deadline = asyncio.get_event_loop().time() + 8
                while len(seen) < 3 and asyncio.get_event_loop().time() < deadline:
                    await asyncio.sleep(0.05)
                client.stop()
                await asyncio.wait_for(task, timeout=5)
                return client

        client = asyncio.run(go())
        self.assertEqual(seen, [("KNYC", 17.0, True), ("KNYC", 18.0, False), ("KNYC", 19.0, False)])
        self.assertEqual(client.latest["KNYC"].temp_celsius, 19.0)
        self.assertEqual(client.state.plan, "starter")
        self.assertGreaterEqual(connections["n"], 2)
        subs = [e for e in server_log if e[0] == "subscribe"]
        self.assertEqual([s[1] for s in subs][:2], [1, 2])
        self.assertEqual(subs[0][2], ("metar.obs.knyc",))
        self.assertIn("key=test-key", server_log[0][2])


class TestTlsContext(unittest.TestCase):
    def test_verify_on_uses_certifi_or_system_and_requires_certs(self):
        import ssl

        from core.metar_ws_client import build_ssl_context

        ctx = build_ssl_context(MetarWsConfig(api_key="k"))
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_custom_bundle_is_used(self):
        import ssl

        from core.metar_ws_client import build_ssl_context

        import certifi

        with tempfile.TemporaryDirectory() as d:
            bundle = Path(d) / "ca.pem"
            bundle.write_bytes(Path(certifi.where()).read_bytes())
            ctx = build_ssl_context(MetarWsConfig(api_key="k", ca_bundle=str(bundle)))
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            self.assertGreater(len(ctx.get_ca_certs()), 50)

    def test_verify_off_only_when_asked(self):
        import ssl

        from core.metar_ws_client import build_ssl_context

        ctx = build_ssl_context(MetarWsConfig(api_key="k", ssl_verify=False))
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)

    def test_city_channel_prefers_raw_icao_over_channel_map(self):
        obs = normalize_publication(CITY_FRAME, {"metar.obs.london": "XXXX"})
        self.assertEqual(obs.icao_id, "EGLL")

    def test_receipt_and_sent_times_give_platform_latency(self):
        frame = {"type": "publication", "channel": "metar.obs.klga", "cached": False, "sent_at": "2026-10-25T12:53:27.150Z",
                 "data": {"station": "KLGA", "temp_c": "11", "report_time": "2026-10-25T12:51:00Z", "receipt_time": "2026-10-25T12:53:27.135Z",
                          "raw": "METAR KLGA 251251Z 33003KT 10SM CLR 11/04 A3056", "report_type": "METAR"}}
        obs = normalize_publication(frame)
        self.assertEqual(obs.platform_latency_ms, 15)
        self.assertEqual(obs.report_type, "METAR")
        d = obs.to_dict()
        self.assertEqual(d["platform_latency_ms"], 15)
        back = MetarObservation.from_dict(d)
        self.assertEqual(back.platform_latency_ms, 15)
        self.assertEqual(back.received_at_utc, obs.received_at_utc)

    def test_recent_latencies_only_for_live_frames(self):
        client = MetarWsClient(MetarWsConfig(api_key="k", forecasts=False))
        client.set_stations(["KLGA"])
        live = {"type": "publication", "channel": "metar.obs.klga", "cached": False, "sent_at": "2026-10-25T12:53:27.150Z",
                "data": {"station": "KLGA", "temp_c": "11", "report_time": "2026-10-25T12:51:00Z", "receipt_time": "2026-10-25T12:53:27.135Z", "raw": "METAR KLGA 251251Z 11/04"}}
        cached = dict(live, cached=True, data=dict(live["data"], report_time="2026-10-25T11:51:00Z"))

        async def go():
            await client.handle_message(cached)
            await client.handle_message(live)

        asyncio.run(go())
        self.assertEqual(len(client.state.recent_latencies), 1)
        self.assertEqual(client.state.recent_latencies[0]["platform_ms"], 15)
        self.assertEqual(client.state.platform_latency_ms(), 15)
        self.assertIsNotNone(client.state.delivery_latency_ms())


if __name__ == "__main__":
    unittest.main()
