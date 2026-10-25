"""Paper mode: bucket decisions, resolution rounding, paper broker + settlement, mapping refresh, cycle runner."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

os.environ.setdefault("DRY_RUN", "1")
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bots.metar_ws import refresh_mapping as rm  # noqa: E402
from core.rt_daily_extrema import Bucket, decide_bucket, decide_extrema  # noqa: E402
from core.station_day_state import DayStateTracker, resolution_value  # noqa: E402
from core.metar_observation import MetarObservation  # noqa: E402
from execution import paper_broker as pb  # noqa: E402

NY = ZoneInfo("America/New_York")


class TestBucket(unittest.TestCase):
    def test_from_mapping_forms(self):
        self.assertEqual(Bucket.from_mapping({"threshold_low": 16, "threshold_high": 16}), Bucket(16.0, 16.0))
        self.assertEqual(Bucket.from_mapping({"threshold_low": None, "threshold_high": 15}), Bucket(None, 15.0))
        self.assertEqual(Bucket.from_mapping({"threshold": 80, "yes_if_above": True}), Bucket(80.0, None))
        self.assertEqual(Bucket.from_mapping({"threshold": 80, "yes_if_above": False}), Bucket(None, 79.0))
        self.assertIsNone(Bucket.from_mapping({}))
        self.assertTrue(Bucket(16.0, 16.0).contains(16))
        self.assertFalse(Bucket(16.0, 16.0).contains(17))

    def test_max_buckets(self):
        noon = datetime(2026, 10, 25, 12, 0, tzinfo=NY)
        late = datetime(2026, 10, 25, 23, 59, tzinfo=NY)
        exact = Bucket(16.0, 16.0)
        self.assertEqual(decide_bucket(variable="temp_max", bucket=exact, extreme_value=17, local_now=noon).binary_state, "dead")
        self.assertEqual(decide_bucket(variable="temp_max", bucket=exact, extreme_value=16, local_now=noon).binary_state, "open")
        self.assertEqual(decide_bucket(variable="temp_max", bucket=exact, extreme_value=16, local_now=late).binary_state, "met")
        self.assertEqual(decide_bucket(variable="temp_max", bucket=exact, extreme_value=14, local_now=late).binary_state, "dead")
        self.assertEqual(decide_bucket(variable="temp_max", bucket=Bucket(25.0, None), extreme_value=25, local_now=noon).binary_state, "met")
        self.assertEqual(decide_bucket(variable="temp_max", bucket=Bucket(None, 15.0), extreme_value=16, local_now=noon).binary_state, "dead")
        self.assertEqual(decide_bucket(variable="temp_max", bucket=Bucket(None, 15.0), extreme_value=14, local_now=noon).binary_state, "open")
        self.assertEqual(decide_bucket(variable="temp_max", bucket=Bucket(None, 15.0), extreme_value=14, local_now=late).binary_state, "met")

    def test_min_buckets_and_legacy_wrapper(self):
        noon = datetime(2026, 10, 25, 12, 0, tzinfo=NY)
        late = datetime(2026, 10, 25, 23, 59, tzinfo=NY)
        exact = Bucket(5.0, 5.0)
        self.assertEqual(decide_bucket(variable="temp_min", bucket=exact, extreme_value=4, local_now=noon).binary_state, "dead")
        self.assertEqual(decide_bucket(variable="temp_min", bucket=exact, extreme_value=5, local_now=noon).binary_state, "open")
        self.assertEqual(decide_bucket(variable="temp_min", bucket=exact, extreme_value=5, local_now=late).binary_state, "met")
        self.assertEqual(decide_bucket(variable="temp_min", bucket=Bucket(None, 3.0), extreme_value=2, local_now=noon).binary_state, "met")
        d = decide_extrema(variable="temp_max", yes_if_above=True, threshold=70, extreme_value=71, local_now=noon)
        self.assertEqual((d.binary_state, d.recommended_side), ("met", "YES"))

    def test_resolution_value_rounding_matches_market_examples(self):
        # Celsius markets: 13.9 -> 14, 17.4 -> 17, 16.5 -> 17, -1.7 -> -2, -8.5 -> -8
        self.assertEqual([resolution_value(v, "C") for v in (13.9, 17.4, 16.5, -1.7, -8.5)], [14, 17, 17, -2, -8])
        # Fahrenheit markets: 18.3C -> 65, 17.8C -> 64, 19C -> 66, -22.5C -> -8
        self.assertEqual([resolution_value(v, "F") for v in (18.3, 17.8, 19, -22.5)], [65, 64, 66, -8])
        self.assertIsNone(resolution_value(None, "F"))

    def test_day_state_uses_t_group_tenths_and_ignores_speci(self):
        tr = DayStateTracker(lambda icao: "America/New_York")
        raw = "METAR KNYC 251851Z 17/06 A3012 RMK AO2 T01720061"
        tr.ingest(MetarObservation("KNYC", 17, "2026-10-25T18:51:00Z", raw, datetime(2026, 10, 25, 18, 51, tzinfo=timezone.utc), False))
        tr.ingest(MetarObservation("KNYC", 19, "2026-10-25T19:12:00Z", "SPECI KNYC 251912Z 19/06 A3012 RMK AO2 T01890061",
                                   datetime(2026, 10, 25, 19, 12, tzinfo=timezone.utc), True))
        st = tr.get("KNYC", "2026-10-25")
        self.assertEqual((st.max_c, st.max_source, st.lead_max_c, st.lead_max_is_speci), (17.2, "t_group", 18.9, True))
        self.assertEqual(resolution_value(st.max_c, "F"), 63)


class TestPaperBroker(unittest.TestCase):
    def test_fill_dedupe_and_settlement(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "paper.sqlite"
            now = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
            self.assertTrue(pb.record_paper_fill(db, market_id="0xaaa", side="YES", price=0.94, shares=50, now=now))
            self.assertFalse(pb.record_paper_fill(db, market_id="0xaaa", side="YES", price=0.95, shares=10, now=now))
            self.assertTrue(pb.record_paper_fill(db, market_id="0xbbb", side="NO", price=0.97, shares=20, now=now))
            self.assertTrue(pb.record_paper_fill(db, market_id="0xccc", side="YES", price=0.5, shares=10, now=now))

            def fake_fetch(ids):
                return {
                    "0xaaa": {"closed": True, "outcomePrices": '["1", "0"]', "outcomes": '["Yes", "No"]'},
                    "0xbbb": {"closed": True, "outcomePrices": '["1", "0"]'},        # NO bought, YES won -> loss
                    "0xccc": {"closed": True, "outcomePrices": '["0.5", "0.5"]'},    # closed but not final
                }

            res = pb.settle_paper_positions(db, fetch=fake_fetch, now=now)
            self.assertEqual((res["settled"], res["open"]), (2, 1))
            self.assertAlmostEqual(res["pnl"], 50 * (1 - 0.94) - 20 * 0.97, places=4)
            with closing(sqlite3.connect(db)) as conn:
                rows = {r[0]: r for r in conn.execute("SELECT market_id, status, pnl FROM positions").fetchall()}
            self.assertEqual(rows["0xaaa"][1], "SETTLED")
            self.assertAlmostEqual(rows["0xaaa"][2], 3.0, places=4)
            self.assertAlmostEqual(rows["0xbbb"][2], -19.4, places=4)
            self.assertEqual(rows["0xccc"][1], "OPEN")

    def test_final_outcome(self):
        self.assertEqual(pb.final_outcome({"closed": True, "outcomePrices": '["0", "1"]'}), "NO")
        self.assertIsNone(pb.final_outcome({"closed": False, "outcomePrices": '["1", "0"]'}))
        self.assertIsNone(pb.final_outcome({"closed": True, "outcomePrices": "garbage"}))

    def test_paper_enabled_env(self):
        with mock.patch.dict(os.environ, {"PAPER_TRADING": "0"}):
            self.assertFalse(pb.paper_enabled())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PAPER_TRADING", None)
            self.assertTrue(pb.paper_enabled())


class TestRefreshMapping(unittest.TestCase):
    EVENTS = [
        {
            "title": "Highest temperature in NYC on October 26?", "slug": "highest-temperature-in-nyc-on-october-26", "endDate": "2026-10-26T12:00:00Z",
            "markets": [
                {"conditionId": "0x1", "groupItemTitle": "60°F or below", "question": "q1", "slug": "m1", "resolutionSource": "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA"},
                {"conditionId": "0x2", "groupItemTitle": "61°F", "question": "q2", "slug": "m2", "resolutionSource": "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA"},
                {"conditionId": "0x3", "groupItemTitle": "62-63°F", "question": "q3", "slug": "m3", "resolutionSource": "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA"},
                {"conditionId": "0x4", "groupItemTitle": "64°F or higher", "question": "q4", "slug": "m4", "resolutionSource": "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA"},
                {"conditionId": "0x5", "groupItemTitle": "weird", "question": "q5", "slug": "m5", "resolutionSource": "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA"},
            ],
        },
        {"title": "Lowest temperature in Jinan on October 26?", "endDate": "2026-10-26T12:00:00Z",
         "markets": [{"conditionId": "0x9", "groupItemTitle": "5°C", "resolutionSource": "https://www.wunderground.com/history/daily/cn/jinan/ZSJN"}]},
        {"title": "Highest temperature in Paris on October 20?", "endDate": "2026-10-20T12:00:00Z",
         "markets": [{"conditionId": "0x8", "groupItemTitle": "15°C", "resolutionSource": "https://www.wunderground.com/history/daily/fr/paris/LFPB"}]},
        {"title": "Precipitation in London in October?", "markets": []},
    ]
    SERVED = {"KLGA": {"timezone": "America/New_York"}, "LFPB": {"timezone": "Europe/Paris"}}

    def test_parsers(self):
        self.assertEqual(rm.parse_bucket("15°C or below"), {"low": None, "high": 15.0, "unit": "C"})
        self.assertEqual(rm.parse_bucket("70°F or higher"), {"low": 70.0, "high": None, "unit": "F"})
        self.assertEqual(rm.parse_bucket("60-61°F"), {"low": 60.0, "high": 61.0, "unit": "F"})
        self.assertEqual(rm.parse_bucket("16°C"), {"low": 16.0, "high": 16.0, "unit": "C"})
        self.assertIsNone(rm.parse_bucket("Yes"))
        self.assertEqual(rm.parse_title("Highest temperature in NYC on October 26?", "2026-10-26T12:00:00Z"),
                         {"variable": "temp_max", "city": "NYC", "target_date": "2026-10-26"})
        self.assertEqual(rm.parse_title("Lowest temperature in Buenos Aires on November 2, 2026?", None)["target_date"], "2026-11-02")
        self.assertIsNone(rm.parse_title("Precipitation in London in October?", None))
        self.assertEqual(rm.icao_from_resolution_source("https://www.wunderground.com/history/daily/cn/jinan/ZSJN"), "ZSJN")
        self.assertEqual(rm.icao_from_resolution_source("https://aviationweather.gov/api/data/metar?ids=KLGA&format=raw&date=2026-10-26T07:00:00Z&hours=30"), "KLGA")
        self.assertEqual(rm.icao_from_resolution_source(""), "")

    def test_build_and_merge(self):
        entries, stats = rm.build_entries(self.EVENTS, self.SERVED, today=date(2026, 10, 25))
        self.assertEqual(set(entries), {"0x1", "0x2", "0x3", "0x4"})
        self.assertEqual(stats["unserved"], {"ZSJN": "Jinan"})
        self.assertEqual((stats["unparsed"], stats["past"]), (1, 1))
        e = entries["0x3"]
        self.assertEqual((e["icao"], e["timezone"], e["threshold_low"], e["threshold_high"], e["threshold_unit"]), ("KLGA", "America/New_York", 62.0, 63.0, "F"))
        self.assertEqual((entries["0x1"]["threshold"], entries["0x1"]["yes_if_above"]), (60.0, False))
        self.assertEqual((entries["0x4"]["threshold"], entries["0x4"]["yes_if_above"]), (64.0, True))
        existing = {
            "_rt_note": {"x": "y"},
            "0xmanual": {"icao": "KORD", "threshold": 80, "target_date": "2026-03-26"},
            "0xold": {"icao": "KORD", "threshold": 80, "target_date": "2026-10-20", "source": "gamma"},
            "0x1": {"icao": "KLGA", "source": "gamma", "target_date": "2026-10-26", "stale": True},
        }
        merged = rm.merge_mapping(existing, entries, today=date(2026, 10, 25))
        self.assertIn("_rt_note", merged)
        self.assertIn("0xmanual", merged)
        self.assertNotIn("0xold", merged)
        self.assertNotIn("stale", merged["0x1"])
        self.assertEqual(len([k for k in merged if not k.startswith("_")]), 5)


class TestRunCycle(unittest.TestCase):
    def test_summary_line_written(self):
        from bots.metar_ws import run_cycle as rc

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "cycle.log"
            meta = Path(d) / "meta.json"
            meta.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
            calls = []

            def fake_run(script, logp):
                calls.append(script)
                return 0

            env = {"UPDATE_BOT_RT_CYCLE_LOG": str(log), "UPDATE_BOT_RT_SKIP_SETTLEMENT": "1", "UPDATE_BOT_RT_PIPELINE": "bot2"}
            with mock.patch.dict(os.environ, env), mock.patch.object(rc, "_run", fake_run), \
                 mock.patch("core.bot2_data_paths.get_rt_paths", lambda: {"meta": meta}):
                self.assertEqual(rc.run_cycle(), 0)
            self.assertEqual(calls, ["bots/bot2_realtime/metar_live/produce_rt_signals.py", "bots/bot2_realtime/execution/finance_engine_rt.py"])
            from ui.history import parse_cycle_summaries

            rows = parse_cycle_summaries(log.read_text(encoding="utf-8").splitlines())
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["ok"])
            self.assertEqual(rows[0]["finance"], 0)
            compact = (log.parent / "cycle_summaries.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(compact), 1)
            self.assertEqual(parse_cycle_summaries(compact)[0]["ts"], rows[0]["ts"])


if __name__ == "__main__":
    unittest.main()
