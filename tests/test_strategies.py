"""strategies/: config layering, sniper and forecast_edge intents, engine consumption of intents."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

os.environ.setdefault("DRY_RUN", "1")
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.forecast_state import ForecastRecord  # noqa: E402
from core.rt_daily_extrema import Bucket  # noqa: E402
from strategies import Intent, MarketContext, REGISTRY, load_config, load_strategies  # noqa: E402
from strategies.forecast_edge import ForecastEdgeStrategy  # noqa: E402
from strategies.sniper import SniperStrategy  # noqa: E402

NY = ZoneInfo("America/New_York")


def rec(model, tier, mx, complete=True):
    return ForecastRecord("KLGA", model, tier, "x", datetime(2026, 10, 25, 6, tzinfo=timezone.utc), "2026-10-25", mx, 5.0,
                          datetime(2026, 10, 25, 20, tzinfo=timezone.utc), None, complete, "day_complete", None, hours_available=48)


def ctx(**kw):
    base = dict(
        market_id="0xabc", icao="KLGA", variable="temp_max", unit="F", bucket=Bucket(62.0, 63.0), target_date="2026-10-25",
        timezone="America/New_York", local_now=datetime(2026, 10, 25, 12, 0, tzinfo=NY), deadline_hhmm="23:59", dead_min_gap=15.0,
        day_end_utc=datetime(2026, 10, 26, 4, tzinfo=timezone.utc), extreme_c=16.1, resolution_value=61, coverage_ok=True, obs_count=8,
        forecasts={"gfs": rec("gfs", "basic", 17.5), "hrrr": rec("hrrr", "hires", 17.2)},
    )
    base.update(kw)
    return MarketContext(**base)


class TestConfig(unittest.TestCase):
    def test_defaults_file_and_env_layering(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "strategies.json"
            p.write_text(json.dumps({"strategies": {"forecast_edge": {"min_edge": 0.12, "enabled": True}}}), encoding="utf-8")
            cfg = load_config(p, env={})
            self.assertEqual(cfg.params("forecast_edge")["min_edge"], 0.12)
            self.assertTrue(cfg.params("forecast_edge")["enabled"])
            self.assertEqual(cfg.params("sniper")["max_bet_usdc"], 50.0)
            cfg2 = load_config(p, env={"FINANCE_RT_PROB_TRADING": "0", "FINANCE_RT_MAX_BET_USDC": "20", "FINANCE_RT_PROB_MIN_EDGE": "0.05"})
            self.assertFalse(cfg2.params("forecast_edge")["enabled"])
            self.assertEqual(cfg2.params("sniper")["max_bet_usdc"], 20.0)
            self.assertEqual(cfg2.params("forecast_edge")["min_edge"], 0.05)
            self.assertEqual(cfg2.enabled_names(), ["sniper"])
        missing = load_config(Path(d) / "nope.json", env={})
        self.assertIsNone(missing.path)
        self.assertEqual(set(REGISTRY), {"sniper", "forecast_edge"})
        self.assertEqual([s.name for s in load_strategies(load_config(Path(d) / "nope.json", env={}))], ["sniper", "forecast_edge"])


    def test_operator_overrides_win_over_file_and_env(self):
        from strategies import load_overrides, set_override

        with tempfile.TemporaryDirectory() as d:
            cfg_p = Path(d) / "strategies.json"
            cfg_p.write_text(json.dumps({"strategies": {"forecast_edge": {"enabled": False}}}), encoding="utf-8")
            ov_p = Path(d) / "overrides.json"
            env = {"FINANCE_RT_PROB_TRADING": "1"}
            cfg = load_config(cfg_p, env=env, overrides=ov_p)
            self.assertTrue(cfg.params("forecast_edge")["enabled"])
            self.assertEqual(cfg.enabled_source, {"sniper": "default", "forecast_edge": "env"})
            set_override("forecast_edge", False, path=ov_p, who="test")
            set_override("sniper", False, path=ov_p, who="test")
            cfg = load_config(cfg_p, env=env, overrides=ov_p)
            self.assertFalse(cfg.params("forecast_edge")["enabled"])
            self.assertEqual(cfg.enabled_names(), [])
            self.assertEqual(cfg.enabled_source["forecast_edge"], "override")
            self.assertEqual(load_overrides(ov_p)["sniper"]["set_by"], "test")
            set_override("sniper", None, path=ov_p)  # reset -> back to defaults/file/env
            cfg = load_config(cfg_p, env=env, overrides=ov_p)
            self.assertEqual(cfg.enabled_names(), ["sniper"])
            self.assertEqual(cfg.enabled_source["sniper"], "default")
            self.assertNotIn("sniper", load_overrides(ov_p))
            self.assertFalse((ov_p.with_suffix(".json.tmp")).exists())


class TestSniper(unittest.TestCase):
    def test_states_and_intents(self):
        s = SniperStrategy({"enabled": True, "max_best_ask": 0.97, "max_bet_usdc": 40})
        dead = s.evaluate(ctx(bucket=Bucket(58.0, 59.0)))  # resolution 61 is past 58-59
        self.assertEqual((dead.fields["binary_state"], dead.intent.side, dead.intent.p, dead.intent.kind), ("dead", "NO", 1.0, "binary"))
        self.assertEqual((dead.intent.max_best_ask, dead.intent.max_bet_usdc), (0.97, 40))
        met = s.evaluate(ctx(bucket=Bucket(60.0, None)))  # open-ended bucket reached
        self.assertEqual((met.fields["binary_state"], met.intent.side), ("met", "YES"))
        open_ = s.evaluate(ctx())  # 61 inside 62-63? no: below -> open, no intent
        self.assertEqual((open_.fields["binary_state"], open_.intent), ("open", None))
        self.assertEqual(s.evaluate(ctx(extreme_c=None, resolution_value=None)).fields["decision_reason"], "no day state yet")
        off = SniperStrategy({"enabled": False}).evaluate(ctx(bucket=Bucket(58.0, 59.0)))
        self.assertEqual((off.fields["binary_state"], off.intent), ("dead", None))  # fields still computed, no trade


class TestForecastEdge(unittest.TestCase):
    def test_probability_fields_and_intent(self):
        s = ForecastEdgeStrategy({"enabled": True, "min_edge": 0.08, "max_bet_usdc": 25, "require_full_day": True})
        a = s.evaluate(ctx(bucket=Bucket(62.0, 63.0)))
        self.assertIsNotNone(a.fields["p_model"])
        self.assertEqual(a.fields["forecast"]["models"], 2)
        # bucket 62-63F vs forecasts ~17.2-17.5C (63-63.5F): a confident side exists, intent present
        self.assertIsNotNone(a.intent)
        self.assertEqual((a.intent.kind, a.intent.strategy, a.intent.min_edge, a.intent.max_bet_usdc), ("prob", "forecast_edge", 0.08, 25))
        self.assertGreaterEqual(a.intent.p, 0.58)
        # partial day: fields yes, intent no
        b = s.evaluate(ctx(coverage_ok=False))
        self.assertIsNotNone(b.fields["p_model"])
        self.assertIsNone(b.intent)
        self.assertIn("cover", b.fields["forecast_edge_skip"])
        # disabled: fields yes, intent no
        c = ForecastEdgeStrategy({"enabled": False}).evaluate(ctx())
        self.assertIsNotNone(c.fields["p_model"])
        self.assertIsNone(c.intent)
        # no forecasts: nothing
        self.assertEqual(s.evaluate(ctx(forecasts={})).fields, {"p_model": None, "forecast": None})


class TestEngineIntents(unittest.TestCase):
    def test_targets_from_intents_respects_config(self):
        from bots.bot2_realtime.execution.finance_engine_rt import targets_from_intents

        signals = {
            "0xa": {"binary_state": "dead", "intents": [Intent("sniper", "NO", 1.0, "binary", max_bet_usdc=50).to_dict()]},
            "0xb": {"binary_state": "open", "intents": [Intent("forecast_edge", "YES", 0.72, "prob", min_edge=0.08, max_bet_usdc=25).to_dict()]},
            "0xc": {"binary_state": "open", "intents": []},
            "0xd": {"binary_state": "met", "intents": [Intent("sniper", "YES", 1.0, "binary").to_dict()]},
        }
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps({"strategies": {"forecast_edge": {"enabled": True}, "sniper": {"max_targets": 1}}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"STRATEGIES_CONFIG": str(p)}, clear=False):
                for k in ("FINANCE_RT_PROB_TRADING", "FINANCE_RT_MAX_TARGETS_PER_CYCLE", "FINANCE_RT_MAX_BET_USDC", "FINANCE_RT_PROB_MIN_EDGE"):
                    os.environ.pop(k, None)
                targets, found = targets_from_intents(signals)
        self.assertTrue(found)
        self.assertEqual([(t.market_id, t.outcome_side, t.strategy) for t in targets], [("0xd", "YES", "sniper"), ("0xb", "YES", "forecast_edge")])  # sniper capped to 1, YES first
        prob = next(t for t in targets if t.strategy == "forecast_edge")
        self.assertEqual((prob.p_win, prob.min_edge, prob.max_bet_usdc), (0.72, 0.08, 25.0))
        legacy, found2 = targets_from_intents({"0xa": {"binary_state": "dead", "p_win": 0.0}})
        self.assertEqual((legacy, found2), ([], False))


if __name__ == "__main__":
    unittest.main()
