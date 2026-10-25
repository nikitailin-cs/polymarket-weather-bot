"""METAR.ws model forecasts: normalisation, tracker, bucket probability, client routing, engine prob targets."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("DRY_RUN", "1")
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.forecast_state import (  # noqa: E402
    ForecastRecord,
    ForecastTracker,
    bucket_probability,
    celsius_interval_for_bucket,
    forecasts_from_snapshot,
    normalize_forecast_publication,
)
from core.metar_ws_client import MetarWsClient, MetarWsConfig  # noqa: E402

FRAME = {
    "type": "publication", "channel": "metar.forecast.basic.klga", "cached": True, "sent_at": "2026-10-25T12:20:33.201Z",
    "data": {
        "station": "KLGA", "model": "gfs", "tier": "basic", "source": "noaa", "run_utc": "2026-10-25T06:00:00Z",
        "source_available_utc": "2026-10-25T09:32:17Z", "receipt_time": "2026-10-25T11:12:09Z", "local_date": "2026-10-25",
        "temp_max_c": "15.9", "temp_min_c": "11.0", "max_valid_utc": "2026-10-25T20:00:00Z", "min_valid_utc": "2026-10-25T12:00:00Z",
        "available_through_forecast_hour": 384, "local_day_complete": True, "status": "day_complete",
        "grid_point": {"lat": "40.7500", "lon": "286.0000", "distance_km": "10.60"},
        "change": {"previous_run_utc": "2026-10-25T00:00:00Z", "temp_max_delta_c": "-0.1", "temp_min_delta_c": "-0.1"},
    },
}


def rec(model, tier, mx, mn=5.0, run="2026-10-25T06:00:00Z", complete=True, valid="2026-10-25T20:00:00Z", day="2026-10-25"):
    return ForecastRecord("KLGA", model, tier, "x", datetime.fromisoformat(run.replace("Z", "+00:00")), day, mx, mn,
                          datetime.fromisoformat(valid.replace("Z", "+00:00")), datetime(2026, 10, 25, 12, tzinfo=timezone.utc),
                          complete, "day_complete" if complete else "detected", None)


class TestNormalize(unittest.TestCase):
    def test_frame(self):
        r = normalize_forecast_publication(FRAME)
        self.assertEqual((r.station, r.model, r.tier, r.local_date), ("KLGA", "gfs", "basic", "2026-10-25"))
        self.assertEqual((r.temp_max_c, r.temp_min_c, r.complete, r.hours_available), (15.9, 11.0, True, 384))
        self.assertEqual(r.max_delta_c, -0.1)
        self.assertEqual(r.previous_run_utc, datetime(2026, 10, 25, 0, tzinfo=timezone.utc))
        d = r.to_dict()
        self.assertEqual(ForecastRecord.from_dict(d), r)
        self.assertIsNone(normalize_forecast_publication({"type": "publication", "channel": "metar.obs.klga", "data": {}}))


class TestTracker(unittest.TestCase):
    def test_latest_run_wins_and_roundtrip(self):
        tr = ForecastTracker()
        self.assertTrue(tr.ingest(rec("gfs", "basic", 15.9)))
        self.assertFalse(tr.ingest(rec("gfs", "basic", 14.0, run="2026-10-25T00:00:00Z")))  # older run
        self.assertTrue(tr.ingest(rec("gfs", "basic", 16.4, run="2026-10-25T12:00:00Z")))
        self.assertTrue(tr.ingest(rec("hrrr", "hires", 16.1)))
        self.assertEqual(sorted(tr.get("KLGA", "2026-10-25")), ["gfs", "hrrr"])
        self.assertEqual(tr.get("KLGA", "2026-10-25")["gfs"].temp_max_c, 16.4)
        payload = json.loads(json.dumps(tr.to_dict()))
        tr2 = ForecastTracker()
        self.assertEqual(tr2.restore(payload), 2)
        snap = {"forecasts": payload}
        self.assertEqual(forecasts_from_snapshot(snap, "klga", "2026-10-25")["hrrr"].temp_max_c, 16.1)
        self.assertEqual(forecasts_from_snapshot(snap, "KLGA", "2026-10-26"), {})


class TestProbability(unittest.TestCase):
    def test_bucket_interval(self):
        self.assertEqual(celsius_interval_for_bucket(16, 16, "C"), (15.5, 16.5))
        a, b = celsius_interval_for_bucket(60, 61, "F")
        self.assertAlmostEqual(a, (59.5 - 32) * 5 / 9, places=6)
        self.assertAlmostEqual(b, (61.5 - 32) * 5 / 9, places=6)
        self.assertEqual(celsius_interval_for_bucket(None, 15, "C")[0], float("-inf"))

    def test_mixture_sums_to_one_and_conditions_on_observation(self):
        fcs = [rec("gfs", "basic", 15.9), rec("hrrr", "hires", 16.3), rec("icon-eu", "basic", 15.2, complete=False)]
        now = datetime(2026, 10, 25, 14, tzinfo=timezone.utc)
        buckets = [(None, 13), (14, 14), (15, 15), (16, 16), (17, 17), (18, None)]
        ps = [bucket_probability(variable="temp_max", low=lo, high=hi, unit="C", forecasts=fcs, observed_extreme_c=None, now_utc=now).p for lo, hi in buckets]
        self.assertAlmostEqual(sum(ps), 1.0, places=2)
        self.assertEqual(max(range(len(ps)), key=lambda i: ps[i]), 3)  # 16C most likely (gfs 15.9, hrrr 16.3, icon-eu 15.2 at half weight)
        # observed routine max 16.2 so far: buckets below 16 are impossible, 16 contains it
        p_below = bucket_probability(variable="temp_max", low=14, high=14, unit="C", forecasts=fcs, observed_extreme_c=16.2, now_utc=now)
        p_16 = bucket_probability(variable="temp_max", low=16, high=16, unit="C", forecasts=fcs, observed_extreme_c=16.2, now_utc=now)
        p_17 = bucket_probability(variable="temp_max", low=17, high=17, unit="C", forecasts=fcs, observed_extreme_c=16.2, now_utc=now)
        self.assertEqual(p_below.p, 0.0)
        self.assertGreater(p_16.p, 0.5)
        self.assertGreater(p_17.p, 0.0)
        self.assertEqual(p_16.models, 3)  # the partial icon-eu run counts with half weight

    def test_partial_runs_are_forecasts_not_bounds(self):
        now = datetime(2026, 10, 25, 14, tzinfo=timezone.utc)
        only_partial = [rec("hrrr", "hires", 16.3, complete=False)]
        p = bucket_probability(variable="temp_max", low=16, high=16, unit="C", forecasts=only_partial, observed_extreme_c=12.0, now_utc=now)
        self.assertEqual((p.models, p.components[0]["weight"]), (1, 0.5))
        fcs = [rec("gfs", "basic", 14.0), rec("hrrr", "hires", 16.3, complete=False)]
        # the partial hrrr run is one opinion among two, not a certainty: 14C keeps real mass
        p_14 = bucket_probability(variable="temp_max", low=14, high=14, unit="C", forecasts=fcs, observed_extreme_c=12.0, now_utc=now)
        self.assertGreater(p_14.p, 0.1)
        self.assertLess(p_14.p, 0.5)

    def test_run_whose_coverage_ended_is_ignored(self):
        now = datetime(2026, 10, 25, 20, tzinfo=timezone.utc)
        stale = ForecastRecord("KLGA", "hrrr", "hires", "x", datetime(2026, 10, 25, 6, tzinfo=timezone.utc), "2026-10-25", 13.0, 5.0,
                               None, None, False, "detected", None, hours_available=6)
        self.assertIsNone(bucket_probability(variable="temp_max", low=16, high=16, unit="C", forecasts=[stale], observed_extreme_c=15.0, now_utc=now))

    def test_end_of_day_observation_dominates(self):
        fcs = [rec("gfs", "basic", 19.0)]
        late = datetime(2026, 10, 26, 3, tzinfo=timezone.utc)  # one hour before local midnight (New York, UTC-4)
        day_end = datetime(2026, 10, 26, 4, tzinfo=timezone.utc)
        p = bucket_probability(variable="temp_max", low=16, high=16, unit="C", forecasts=fcs, observed_extreme_c=16.1, now_utc=late, day_end_utc=day_end)
        self.assertGreater(p.p, 0.7)
        self.assertTrue(p.components[0]["late"])
        # earlier in the day the same forecast still carries its own weight
        noon = datetime(2026, 10, 25, 16, tzinfo=timezone.utc)
        p2 = bucket_probability(variable="temp_max", low=16, high=16, unit="C", forecasts=fcs, observed_extreme_c=16.1, now_utc=noon, day_end_utc=day_end)
        self.assertLess(p2.p, 0.3)
        self.assertFalse(p2.components[0]["late"])
        # a tracker that only saw the evening must not turn its partial max into the day's answer
        p3 = bucket_probability(variable="temp_max", low=16, high=16, unit="C", forecasts=fcs, observed_extreme_c=16.1, now_utc=late, day_end_utc=day_end, observed_covers_day=False)
        self.assertLess(p3.p, 0.3)
        self.assertFalse(p3.components[0]["late"])
        self.assertIn("do not cover", p3.note)

    def test_min_market(self):
        fcs = [rec("gfs", "basic", 15.0, mn=4.2, valid="2026-10-25T11:00:00Z")]
        now = datetime(2026, 10, 25, 2, tzinfo=timezone.utc)
        p_above = bucket_probability(variable="temp_min", low=8, high=8, unit="C", forecasts=fcs, observed_extreme_c=6.0, now_utc=now)
        p_4 = bucket_probability(variable="temp_min", low=4, high=4, unit="C", forecasts=fcs, observed_extreme_c=6.0, now_utc=now)
        self.assertEqual(p_above.p, 0.0)  # min already 6, cannot end at 8
        self.assertGreater(p_4.p, 0.15)  # sigma 1.9C spreads the mass over ~1C buckets
        self.assertIsNone(bucket_probability(variable="temp_min", low=4, high=4, unit="C", forecasts=[], observed_extreme_c=None))


class TestClientRouting(unittest.TestCase):
    def test_forecast_channels_and_callback(self):
        got = []
        cfg = MetarWsConfig(api_key="k")
        client = MetarWsClient(cfg, on_forecast=lambda r, cached: got.append((r.model, cached)))
        client.set_stations(["KLGA"])
        self.assertEqual(client.wanted_channels(), ["metar.obs.klga", "metar.forecast.basic.klga", "metar.forecast.hires.klga"])
        asyncio.run(client.handle_message(FRAME))
        self.assertEqual(got, [("gfs", True)])
        self.assertEqual(client.forecasts_received, 1)
        self.assertEqual(client.latest, {})  # not an observation
        off = MetarWsClient(MetarWsConfig(api_key="k", forecasts=False))
        off.set_stations(["KLGA"])
        self.assertEqual(off.wanted_channels(), ["metar.obs.klga"])


class TestEngineProbTargets(unittest.TestCase):
    def test_filter_prob_targets(self):
        from bots.bot2_realtime.execution.finance_engine_rt import filter_prob_targets

        signals = {
            "0xa": {"binary_state": "open", "p_model": 0.72},   # YES, conf .72
            "0xb": {"binary_state": "open", "p_model": 0.10},   # NO, conf .90
            "0xc": {"binary_state": "open", "p_model": 0.55},   # too close to coin flip
            "0xd": {"binary_state": "dead", "p_model": 0.01},   # binary path handles it
            "0xe": {"binary_state": "open"},                    # no model
            "0xf": {"binary_state": "open", "p_model": 0.95, "day_extreme": {"coverage_ok": False}},  # partial day: skipped
        }
        with mock.patch.dict(os.environ, {"FINANCE_RT_PROB_MIN_EDGE": "0.08", "FINANCE_RT_MAX_PROB_TARGETS": "10"}):
            t = filter_prob_targets(signals)
        self.assertEqual([(x.market_id, x.outcome_side, round(x.p_win, 2), x.kind) for x in t],
                         [("0xb", "NO", 0.9, "prob"), ("0xa", "YES", 0.72, "prob")])


if __name__ == "__main__":
    unittest.main()
