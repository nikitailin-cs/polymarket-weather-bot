"""Pluggable sources, obs10 frequent observations, the shared AWC registry and the model error table."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("DRY_RUN", "1")
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.forecast_state import sigma_for  # noqa: E402
from core.metar_observation import MetarObservation  # noqa: E402
from core.metar_ws_client import MetarWsClient, MetarWsConfig, is_fast_channel, normalize_publication  # noqa: E402
from core.sources import REGISTRY, Sink, build_sources, source_names_from_env  # noqa: E402
from core.sources.awc_poll import AwcPollSource  # noqa: E402
from core.sources.metar_ws import MetarWsSource  # noqa: E402
from core.station_day_state import DayStateTracker  # noqa: E402

OBS10 = {
    "type": "publication", "channel": "metar.obs10.klga", "cached": False, "sent_at": "2026-10-25T15:35:51.682Z",
    "data": {"station": "KLGA", "report_time": "2026-10-25T15:20:00Z", "receipt_time": "2026-10-25T15:32:14.000Z", "source": "madis",
             "revision": 1, "temp_c": "15", "dewp_c": "4", "wdir": 0, "wspd_kt": "0", "visib_m": 16093, "qnh_hpa": "1035.22"},
}
METAR = {
    "type": "publication", "channel": "metar.obs.klga", "cached": False, "sent_at": "2026-10-25T14:53:32.600Z",
    "data": {"station": "KLGA", "temp_c": "14", "report_time": "2026-10-25T14:51:00Z", "receipt_time": "2026-10-25T14:53:32.572Z",
             "raw": "KLGA 251451Z 29003KT 10SM CLR 14/03 A3057 RMK AO2 SLP352 T01390033", "report_type": "METAR"},
}


class TestObs10(unittest.TestCase):
    def test_normalize_fast_observation(self):
        self.assertTrue(is_fast_channel("metar.obs10.klga"))
        self.assertFalse(is_fast_channel("metar.obs.klga"))
        obs = normalize_publication(OBS10)
        self.assertEqual((obs.icao_id, obs.temp_celsius, obs.report_kind, obs.is_routine, obs.report_type), ("KLGA", 15.0, "obs10", False, "OBS10"))
        self.assertEqual(obs.source, "metar_ws:madis")
        self.assertEqual(obs.platform_latency_ms, 217682)  # receipt 15:32:14.000 -> sent 15:35:51.682
        back = MetarObservation.from_dict(obs.to_dict())
        self.assertEqual((back.report_kind, back.is_routine), ("obs10", False))
        routine = normalize_publication(METAR)
        self.assertEqual((routine.report_kind, routine.is_routine), ("metar", True))

    def test_day_state_keeps_obs10_as_leading_only(self):
        tr = DayStateTracker(lambda icao: "America/New_York")
        tr.ingest(normalize_publication(METAR))
        tr.ingest(normalize_publication(OBS10))
        st = tr.get("KLGA", "2026-10-25")
        self.assertEqual((st.max_c, st.max_source, st.obs_count, st.fast_count), (13.9, "t_group", 1, 1))
        self.assertEqual((st.lead_max_c, st.lead_max_is_speci), (15.0, False))
        self.assertEqual([s[3] for s in st.samples], ["metar", "obs10"])
        # restored state keeps the kind and dedupes a replayed obs10
        tr2 = DayStateTracker(lambda icao: "America/New_York")
        tr2.restore(json.loads(json.dumps(tr.to_dict())))
        tr2.ingest(normalize_publication(OBS10))
        self.assertEqual(tr2.get("KLGA", "2026-10-25").fast_count, 1)

    def test_client_routes_fast_to_latest_fast_and_channels(self):
        client = MetarWsClient(MetarWsConfig(api_key="k", forecasts=False))
        client.set_fast_stations(["KLGA", "KORD"])
        client.set_stations(["KLGA", "EGLL"])
        self.assertEqual(client.wanted_channels(), ["metar.obs.egll", "metar.obs.klga", "metar.obs10.klga"])

        async def go():
            await client.handle_message(METAR)
            await client.handle_message(OBS10)

        asyncio.run(go())
        self.assertEqual(client.latest["KLGA"].temp_celsius, 14.0)
        self.assertEqual(client.latest_fast["KLGA"].temp_celsius, 15.0)
        self.assertEqual(client.fast_received, 1)
        self.assertEqual([r["kind"] for r in client.state.recent_latencies], ["metar", "obs10"])
        off = MetarWsClient(MetarWsConfig(api_key="k", forecasts=False, fast_obs=False))
        off.set_fast_stations(["KLGA"]); off.set_stations(["KLGA"])
        self.assertEqual(off.wanted_channels(), ["metar.obs.klga"])


class TestRegistry(unittest.TestCase):
    def test_names_and_build(self):
        self.assertEqual(set(REGISTRY), {"metar_ws", "awc_poll"})
        with mock.patch.dict(os.environ, {"SOURCES": "metar.ws, awc , metar_ws"}):
            self.assertEqual(source_names_from_env(), ["metar_ws", "awc_poll"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SOURCES", None)
            self.assertEqual(source_names_from_env(), ["metar_ws"])
        srcs = build_sources(["awc_poll", "metar_ws"], metar_ws={"config": MetarWsConfig(api_key="k")}, awc_poll={"interval_sec": 5})
        self.assertIsInstance(srcs[0], AwcPollSource)
        self.assertIsInstance(srcs[1], MetarWsSource)
        with self.assertRaises(KeyError):
            build_sources(["nope"])


class TestAwcPollSource(unittest.TestCase):
    def test_polls_and_delivers_new_rows_once(self):
        rows = [
            {"icaoId": "KORD", "temp": 12, "dewp": 7, "obsTime": 1761393060, "reportTime": "2026-10-25T10:51:00Z", "rawOb": "KORD 251051Z 17005KT 10SM 12/07 A3041", "receiptTime": "2026-10-25T10:53:00Z"},
            {"icaoId": "KORD", "temp": 13, "obsTime": 1761394800, "reportTime": "2026-10-25T11:20:00Z", "rawOb": "SPECI KORD 251120Z 13/07", "metarType": "SPECI"},
            {"icaoId": "KLGA", "temp": None, "obsTime": 1761393060, "rawOb": "KLGA 251051Z"},
        ]
        calls = []

        def fetcher(icaos):
            calls.append(list(icaos))
            return rows

        got = []
        src = AwcPollSource(interval_sec=0.2, fetcher=fetcher)
        src.set_stations(["KORD", "KLGA"])
        sink = Sink(on_observation=lambda o, cached, name: got.append((o.icao_id, o.report_kind, cached, name)))

        async def go():
            task = asyncio.ensure_future(src.run(sink))
            await asyncio.sleep(0.7)  # at least three polls
            src.stop()
            await asyncio.wait_for(task, 2)

        asyncio.run(go())
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(got, [("KORD", "metar", True, "awc_poll"), ("KORD", "speci", True, "awc_poll")])  # delivered once, first poll flagged cached
        self.assertEqual(src.latest_observations()["KORD"].temp_celsius, 12.0)  # routine only
        st = src.state()
        self.assertTrue(st.connected)
        self.assertEqual(st.messages, 2)
        self.assertEqual(st.details["stations"], 2)


class TestSharedAwcRegistry(unittest.TestCase):
    def test_bot2_alias_and_adapter_shape(self):
        from bots.bot2_realtime.metar_live.realtime_metar_registry import RealTimeMetarRegistry as Bot2
        from core.metar_source import AwcRegistry

        self.assertIs(Bot2, AwcRegistry)
        reg = AwcRegistry()
        obs = MetarObservation("KORD", 12.0, "2026-10-25T10:51:00Z", "KORD 251051Z", datetime(2026, 10, 25, 10, 51, tzinfo=timezone.utc), False)
        with mock.patch.object(reg._inner, "fetch_latest_for_stations", lambda ids: {"KORD": obs}):
            d = reg.fetch_latest_metar("kord")
            self.assertEqual((d["icao"], d["temp_c"], d["temp_f"]), ("KORD", 12.0, 53.6))
            self.assertIsNone(reg.fetch_latest_metar("KLGA"))


class TestModelErrorTable(unittest.TestCase):
    def test_lookup_order(self):
        table = {"tiers": {"hires": 1.3, "basic": 1.9}, "models": {"hrrr": 1.2}, "stations": {"KLGA": {"hrrr": 0.9}}}
        self.assertEqual(sigma_for("hrrr", "hires", "KLGA", table), 0.9)
        self.assertEqual(sigma_for("hrrr", "hires", "KORD", table), 1.2)
        self.assertEqual(sigma_for("unknown-model", "basic", "KORD", table), 1.9)
        self.assertEqual(sigma_for("x", "weird", "", {}), 1.9)  # env defaults
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "errs.json"
            p.write_text(json.dumps({"models": {"gfs": 2.5}}), encoding="utf-8")
            from core.forecast_state import model_error_table

            self.assertEqual(sigma_for("gfs", "basic", "", model_error_table(p)), 2.5)
        shipped = sigma_for("hrrr", "hires", "")
        self.assertGreater(shipped, 0)


if __name__ == "__main__":
    unittest.main()
