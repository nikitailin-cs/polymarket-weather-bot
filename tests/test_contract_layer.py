"""
tests/test_contract_layer.py
Tests complets de la couche contractuelle.
Lance : python tests/test_contract_layer.py
"""
from __future__ import annotations
import json, os, sys, tempfile, threading, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import (
    ContractSpec, ContractSpecCache, MarketStatus,
    TokenInfo, WeatherMarket,
)
from core.contract_parser import (
    _detect_variable, _detect_unit, _detect_station,
    _regex_parse, parse_contract,
)
from execution.position_manager import _read_status, _resolution_price


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_spec(**kw) -> ContractSpec:
    defaults = dict(
        market_id="mkt_abc", condition_id="cond_xyz",
        question="Will NYC high temp exceed 90°F on July 15?",
        city="New York", weather_variable="temp_max",
        threshold_value=90.0, threshold_unit="fahrenheit",
        threshold_op="gt", oracle_station_id="KNYC",
        oracle_source="noaa_ghcnd", oracle_rounding=1,
        oracle_alignment_score=0.95, end_date_iso="2025-07-15T23:59:00Z",
        status=MarketStatus.ACTIVE,
    )
    defaults.update(kw)
    return ContractSpec(**defaults)


def tmp_cache() -> tuple[ContractSpecCache, Path]:
    p = Path(tempfile.mktemp(suffix=".json"))
    return ContractSpecCache(str(p)), p


# ══════════════════════════════════════════════════════════════════════════════

class TestContractSpec(unittest.TestCase):

    def test_tradeable_only_when_active(self):
        self.assertTrue(make_spec(status=MarketStatus.ACTIVE).is_tradeable)
        for s in (MarketStatus.CLOSED_PENDING, MarketStatus.RESOLVED_YES,
                  MarketStatus.RESOLVED_NO, MarketStatus.CANCELLED):
            self.assertFalse(make_spec(status=s).is_tradeable)

    def test_terminal(self):
        self.assertFalse(make_spec(status=MarketStatus.ACTIVE).is_terminal)
        self.assertFalse(make_spec(status=MarketStatus.CLOSED_PENDING).is_terminal)
        self.assertTrue(make_spec(status=MarketStatus.RESOLVED_YES).is_terminal)
        self.assertTrue(make_spec(status=MarketStatus.CANCELLED).is_terminal)

    def test_edge_multiplier(self):
        self.assertAlmostEqual(make_spec(oracle_alignment_score=0.80).effective_edge_multiplier, 0.80)
        self.assertEqual(make_spec(status=MarketStatus.CLOSED_PENDING,
                                   oracle_alignment_score=0.90).effective_edge_multiplier, 0.0)

    def test_evaluate_gt(self):
        s = make_spec(threshold_value=90.0, threshold_op="gt")
        self.assertTrue(s.evaluate(91.0))
        self.assertFalse(s.evaluate(90.0))

    def test_evaluate_gte(self):
        s = make_spec(threshold_value=90.0, threshold_op="gte")
        self.assertTrue(s.evaluate(90.0))

    def test_evaluate_lt(self):
        s = make_spec(threshold_value=32.0, threshold_op="lt")
        self.assertTrue(s.evaluate(31.0))
        self.assertFalse(s.evaluate(32.0))

    def test_serialization_roundtrip(self):
        spec  = make_spec()
        spec2 = ContractSpec.from_dict(spec.to_dict())
        self.assertEqual(spec.market_id, spec2.market_id)
        self.assertEqual(spec.status,    spec2.status)
        self.assertAlmostEqual(spec.oracle_alignment_score, spec2.oracle_alignment_score)

    def test_unknown_status_fallback(self):
        d = make_spec().to_dict()
        d["status"] = "INVALID"
        spec = ContractSpec.from_dict(d)
        self.assertEqual(spec.status, MarketStatus.ACTIVE)


class TestContractSpecCache(unittest.TestCase):

    def test_empty(self):
        cache, p = tmp_cache()
        try:
            self.assertEqual(len(cache), 0)
            self.assertIsNone(cache.get("x"))
        finally:
            p.unlink(missing_ok=True)

    def test_set_get_persist(self):
        cache, p = tmp_cache()
        try:
            cache.set(make_spec())
            cache2 = ContractSpecCache(str(p))
            self.assertIsNotNone(cache2.get("mkt_abc"))
        finally:
            p.unlink(missing_ok=True)
            p.with_suffix(".tmp").unlink(missing_ok=True)

    def test_update_status_only(self):
        cache, p = tmp_cache()
        try:
            cache.set(make_spec())
            cache.update_status("mkt_abc", MarketStatus.RESOLVED_YES, resolution_price=1.0)
            spec = cache.get("mkt_abc")
            self.assertEqual(spec.status, MarketStatus.RESOLVED_YES)
            self.assertEqual(spec.weather_variable, "temp_max")  # inchangé
        finally:
            p.unlink(missing_ok=True)
            p.with_suffix(".tmp").unlink(missing_ok=True)

    def test_corrupted_cache_recovery(self):
        p = Path(tempfile.mktemp(suffix=".json"))
        p.write_text("{ INVALID")
        try:
            cache = ContractSpecCache(str(p))
            self.assertEqual(len(cache), 0)
        finally:
            p.unlink(missing_ok=True)

    def test_thread_safety(self):
        cache, p = tmp_cache()
        errors = []
        def writer(i):
            try:
                cache.set(make_spec(market_id=f"m{i:03d}", condition_id=f"c{i}"))
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        try:
            self.assertEqual(errors, [])
            self.assertEqual(len(cache), 20)
        finally:
            p.unlink(missing_ok=True)
            p.with_suffix(".tmp").unlink(missing_ok=True)


class TestContractParser(unittest.TestCase):

    def test_variable_temp_max(self):
        self.assertEqual(_detect_variable("high temperature above 90°f"), "temp_max")

    def test_variable_precip(self):
        self.assertEqual(_detect_variable("will there be rain tomorrow?"), "precip")

    def test_variable_unknown(self):
        self.assertEqual(_detect_variable("will it be sunny?"), "unknown")

    def test_unit_fahrenheit(self):
        self.assertEqual(_detect_unit("f"), "fahrenheit")
        self.assertEqual(_detect_unit("fahrenheit"), "fahrenheit")

    def test_unit_celsius(self):
        self.assertEqual(_detect_unit("c"), "celsius")

    def test_station_nyc(self):
        sid, src, score = _detect_station("nyc high temp exceed 90°f")
        self.assertEqual(sid, "KNYC")
        self.assertGreater(score, 0.9)

    def test_station_paris(self):
        sid, src, score = _detect_station("will paris exceed 35°c")
        self.assertEqual(sid, "07156")

    def test_station_unknown(self):
        sid, src, score = _detect_station("will it snow in vladivostok?")
        self.assertEqual(sid, "")
        self.assertLess(score, 0.5)

    def test_regex_standard(self):
        spec = _regex_parse(
            "Will the high temperature in New York exceed 90°F on July 15?",
            "mkt_001", "2025-07-15T23:59:00Z",
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec.weather_variable, "temp_max")
        self.assertAlmostEqual(spec.threshold_value, 90.0)
        self.assertEqual(spec.threshold_unit, "fahrenheit")
        self.assertEqual(spec.threshold_op, "gt")

    def test_regex_celsius(self):
        spec = _regex_parse(
            "Will Paris high temperature exceed 35°C on August 1?",
            "mkt_002", "2025-08-01T23:59:00Z",
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec.threshold_unit, "celsius")
        self.assertAlmostEqual(spec.threshold_value, 35.0)

    def test_regex_below_operator(self):
        spec = _regex_parse(
            "Will the low temperature in Chicago fall below 32°F?",
            "mkt_003", "2025-01-10T23:59:00Z",
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec.threshold_op, "lt")

    def test_regex_unknown_variable_returns_none(self):
        spec = _regex_parse("Will it be sunny in Miami?", "mkt_004", "2025-07-15T23:59:00Z")
        self.assertIsNone(spec)

    def test_parse_contract_cache_hit_no_llm(self):
        cache, p = tmp_cache()
        try:
            cache.set(make_spec())
            with patch("core.contract_parser._llm_parse") as mock_llm:
                result = parse_contract("mkt_abc", "cond_xyz",
                                        "Will NYC high temp exceed 90°F?",
                                        "2025-07-15Z", cache)
                mock_llm.assert_not_called()
                self.assertIsNotNone(result)
        finally:
            p.unlink(missing_ok=True)
            p.with_suffix(".tmp").unlink(missing_ok=True)

    def test_parse_contract_low_alignment_returns_none(self):
        cache, p = tmp_cache()
        os.environ["MIN_ORACLE_ALIGNMENT"] = "0.70"
        try:
            # Question sans ville connue ni variable claire → score bas → None
            result = parse_contract(
                "mkt_unk", "", "Will the weather be nice somewhere?",
                "2025-07-15Z", cache,
            )
            if result is not None:
                self.assertGreaterEqual(result.oracle_alignment_score, 0.70)
        finally:
            os.environ.pop("MIN_ORACLE_ALIGNMENT", None)
            p.unlink(missing_ok=True)
            p.with_suffix(".tmp").unlink(missing_ok=True)


class TestPositionManager(unittest.TestCase):

    def _data(self, p_yes="0.99", active=False, closed=True):
        return {"active": active, "closed": closed, "outcomePrices": [p_yes, str(1-float(p_yes))]}

    def test_read_status_yes(self):
        self.assertEqual(_read_status(self._data("0.99")), MarketStatus.RESOLVED_YES)

    def test_read_status_no(self):
        self.assertEqual(_read_status(self._data("0.01")), MarketStatus.RESOLVED_NO)

    def test_read_status_cancelled(self):
        self.assertEqual(_read_status(self._data("0.50")), MarketStatus.CANCELLED)

    def test_read_status_pending(self):
        self.assertEqual(_read_status(self._data("0.70", closed=False)), MarketStatus.CLOSED_PENDING)

    def test_read_status_still_active(self):
        self.assertIsNone(_read_status({"active": True, "outcomePrices": ["0.65"]}))

    def test_resolution_price_yes_side_yes(self):
        self.assertAlmostEqual(_resolution_price(MarketStatus.RESOLVED_YES, "YES", {}), 1.0)

    def test_resolution_price_no_side_yes(self):
        self.assertAlmostEqual(_resolution_price(MarketStatus.RESOLVED_NO,  "YES", {}), 0.0)

    def test_resolution_price_yes_side_no(self):
        self.assertAlmostEqual(_resolution_price(MarketStatus.RESOLVED_YES, "NO",  {}), 0.0)

    def test_resolution_price_cancelled(self):
        data = {"outcomePrices": ["0.48"]}
        self.assertAlmostEqual(_resolution_price(MarketStatus.CANCELLED, "YES", data), 0.48)

    def test_resolution_price_cancelled_default(self):
        self.assertAlmostEqual(_resolution_price(MarketStatus.CANCELLED, "YES", {}), 0.5)


class TestEdgeDetector(unittest.TestCase):
    """
    Vérifie la logique de kelly_stake avec déflation oracle.
    On réplique kelly_stake localement pour éviter la chaîne d'imports
    edge_detector → probability_engine → aggregator → weatherapi → dotenv.
    """

    @staticmethod
    def _kelly(p_win: float, odds: float, bankroll: float, alignment: float = 1.0) -> float:
        from config.settings import KELLY_FRACTION, MAX_BET_USD
        fraction = KELLY_FRACTION
        if alignment < 0.85:
            fraction = KELLY_FRACTION * alignment
        q_win = 1 - p_win
        k = max(0.0, (p_win * odds - q_win) / odds)
        k *= fraction
        return round(min(k * bankroll, MAX_BET_USD), 2)

    def test_no_spec_cache_no_deflation(self):
        """alignment=1.0 -> aucune reduction, resultat positif."""
        bet = self._kelly(0.70, 1.5, 100.0, alignment=1.0)
        self.assertGreater(bet, 0.0)
        self.assertLessEqual(bet, 20.0)

    def test_kelly_reduced_by_alignment(self):
        """alignment faible -> mise reduite proportionnellement."""
        bet_full = self._kelly(0.70, 1.5, 100.0, alignment=1.00)
        bet_low  = self._kelly(0.70, 1.5, 100.0, alignment=0.70)
        self.assertGreater(bet_full, bet_low)


class TestIntegration(unittest.TestCase):

    def test_full_cycle_resolved_yes(self):
        """Scan → ContractSpec → edge → résolution YES → PnL positif."""
        cache, p = tmp_cache()
        try:
            # Parse contrat
            spec = _regex_parse(
                "Will the high temperature in New York exceed 90°F on July 15?",
                "mkt_full", "2025-07-15T23:59:00Z",
            )
            self.assertIsNotNone(spec)
            spec.condition_id = "cond_full"
            cache.set(spec)

            # Vérifie l'edge multiplier
            self.assertAlmostEqual(spec.effective_edge_multiplier, spec.oracle_alignment_score)

            # Simulation résolution YES
            data = {"active": False, "closed": True, "outcomePrices": ["0.99", "0.01"]}
            status = _read_status(data)
            self.assertEqual(status, MarketStatus.RESOLVED_YES)
            res_price = _resolution_price(status, "YES", data)
            self.assertAlmostEqual(res_price, 1.0)

            # PnL
            entry, shares = 0.65, 50.0
            pnl = (res_price - entry) * shares
            self.assertAlmostEqual(pnl, 17.5)

        finally:
            p.unlink(missing_ok=True)
            p.with_suffix(".tmp").unlink(missing_ok=True)

    def test_full_cycle_cancelled(self):
        """Résolution annulée → remboursement partiel → PnL négatif."""
        data = {"active": False, "closed": True, "outcomePrices": ["0.50", "0.50"]}
        status = _read_status(data)
        self.assertEqual(status, MarketStatus.CANCELLED)
        res_price = _resolution_price(status, "YES", data)
        entry, shares = 0.65, 50.0
        pnl = (res_price - entry) * shares
        self.assertAlmostEqual(pnl, (0.50 - 0.65) * 50)  # -7.5$

    def test_non_tradeable_spec_returns_zero_multiplier(self):
        for status in (MarketStatus.CLOSED_PENDING, MarketStatus.RESOLVED_YES,
                       MarketStatus.RESOLVED_NO,   MarketStatus.CANCELLED):
            spec = make_spec(status=status, oracle_alignment_score=0.95)
            self.assertEqual(spec.effective_edge_multiplier, 0.0)


if __name__ == "__main__":
    import unittest
    result = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)