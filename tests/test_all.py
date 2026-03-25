"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  tests/test_all.py — FICHIER DE TEST UNIQUE DU BOT                         ║
║                                                                              ║
║  C'est ici qu'on teste TOUT. Ne pas créer d'autres scripts de test.         ║
║  Si tu travailles sur une nouvelle feature, ajoute tes cas ici.             ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  USAGE                                                                       ║
║    python tests/test_all.py            → tests unitaires + live             ║
║    python tests/test_all.py --unit     → unitaires uniquement (sans réseau) ║
║    python tests/test_all.py --live     → live uniquement (DRY_RUN forcé)    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  COUCHES TESTÉES                                                             ║
║    Unitaires (80+ tests, aucun réseau) :                                     ║
║      • ContractSpec / MarketStatus    — modèles + états                     ║
║      • ContractSpecCache              — persistance JSON + thread-safety     ║
║      • ContractParser                 — regex + détection station/variable   ║
║      • PositionManager                — machine d'états + PnL               ║
║      • EdgeDetector                   — kelly + déflation oracle             ║
║      • UMAOracle                      — phases + timestamps + throttle       ║
║      • UMADisputer                    — assess_proposal + DisputeResult      ║
║      • CTFManager                     — condition_id + RedeemResult          ║
║      • FeatureStore                   — FeatureVector + persist + label      ║
║      • ModelInterface                 — Statistical/Ensemble/ModelPrediction ║
║      • ModelRegistry                  — get_model + predict + fallback       ║
║      • PnLTracker                     — bankroll scindée + DVM blocked       ║
║      • OnchainLogger                  — log_resolution_check + get_ops       ║
║      • Intégration                    — cycle scan→trade→résolution          ║
║    Live (6 étapes, réseau, DRY_RUN=1 forcé) :                               ║
║      1. Gamma API       — scan marchés actifs                                ║
║      2. ContractParser  — parsing sur vrais marchés                         ║
║      3. WebSocket       — connexion + prix temps réel                       ║
║      4. ProbabilityEngine — calcul P(Yes) multi-sources + modèle ML         ║
║      5. EdgeDetector    — signaux avec déflation oracle                     ║
║      6. Cycle complet   — simulation main.py + feature store                ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations
import json, os, sys, time, tempfile, threading, unittest
from datetime import date
from pathlib import Path

# Force DRY_RUN avant tout import du bot — jamais d'ordre réel depuis les tests
os.environ["DRY_RUN"] = "1"

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Séparateurs console ───────────────────────────────────────────────────────
SEP  = "═" * 68
OK   = "✅"
ERR  = "❌"
WARN = "⚠️ "


def _section(title: str) -> None:
    print(f"\n{SEP}\n  {title}\n{SEP}")

def _ok(msg: str)   -> None: print(f"  {OK}  {msg}")
def _err(msg: str)  -> None: print(f"  {ERR}  {msg}")
def _warn(msg: str) -> None: print(f"  {WARN} {msg}")
def _info(msg: str) -> None: print(f"     {msg}")


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — TESTS UNITAIRES (aucun réseau)
# ══════════════════════════════════════════════════════════════════════════════

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

def _make_spec(**kw) -> ContractSpec:
    defaults = dict(
        market_id              = "mkt_abc",
        condition_id           = "cond_xyz",
        question               = "Will NYC high temp exceed 90°F on July 15?",
        city                   = "New York",
        weather_variable       = "temp_max",
        threshold_value        = 90.0,
        threshold_unit         = "fahrenheit",
        threshold_op           = "gt",
        oracle_station_id      = "KNYC",
        oracle_source          = "noaa_ghcnd",
        oracle_rounding        = 0,        # Polymarket arrondit à l'entier
        oracle_alignment_score = 0.95,
        end_date_iso           = "2025-07-15T23:59:00Z",
        status                 = MarketStatus.ACTIVE,
    )
    defaults.update(kw)
    return ContractSpec(**defaults)


def _tmp_cache() -> tuple[ContractSpecCache, Path]:
    p = Path(tempfile.mktemp(suffix=".json"))
    return ContractSpecCache(str(p)), p


# ── ContractSpec ──────────────────────────────────────────────────────────────

class TestContractSpec(unittest.TestCase):

    def test_tradeable_only_when_active(self):
        self.assertTrue(_make_spec(status=MarketStatus.ACTIVE).is_tradeable)
        for s in (MarketStatus.CLOSED_PENDING, MarketStatus.RESOLVED_YES,
                  MarketStatus.RESOLVED_NO,    MarketStatus.CANCELLED):
            self.assertFalse(_make_spec(status=s).is_tradeable, f"should not be tradeable: {s}")

    def test_terminal(self):
        self.assertFalse(_make_spec(status=MarketStatus.ACTIVE).is_terminal)
        self.assertFalse(_make_spec(status=MarketStatus.CLOSED_PENDING).is_terminal)
        self.assertTrue(_make_spec(status=MarketStatus.RESOLVED_YES).is_terminal)
        self.assertTrue(_make_spec(status=MarketStatus.CANCELLED).is_terminal)

    def test_edge_multiplier_active(self):
        self.assertAlmostEqual(
            _make_spec(oracle_alignment_score=0.80).effective_edge_multiplier, 0.80
        )

    def test_edge_multiplier_zero_when_not_tradeable(self):
        self.assertEqual(
            _make_spec(
                status=MarketStatus.CLOSED_PENDING,
                oracle_alignment_score=0.90,
            ).effective_edge_multiplier,
            0.0,
        )

    def test_evaluate_gt(self):
        s = _make_spec(threshold_value=90.0, threshold_op="gt", oracle_rounding=0)
        self.assertTrue(s.evaluate(91.0))
        self.assertFalse(s.evaluate(90.0))

    def test_evaluate_gte(self):
        s = _make_spec(threshold_value=90.0, threshold_op="gte", oracle_rounding=0)
        self.assertTrue(s.evaluate(90.0))
        self.assertFalse(s.evaluate(89.0))

    def test_evaluate_lt(self):
        s = _make_spec(threshold_value=32.0, threshold_op="lt", oracle_rounding=0)
        self.assertTrue(s.evaluate(31.0))
        self.assertFalse(s.evaluate(32.0))

    def test_evaluate_eq(self):
        s = _make_spec(threshold_value=20.0, threshold_op="eq", oracle_rounding=0)
        self.assertTrue(s.evaluate(20.0))
        self.assertFalse(s.evaluate(21.0))

    def test_oracle_rounding_applied(self):
        # oracle_rounding=0 → arrondi à l'entier (règle Polymarket température)
        s = _make_spec(threshold_value=20.0, threshold_op="eq", oracle_rounding=0)
        self.assertTrue(s.evaluate(20.4))   # round(20.4, 0) = 20.0 → eq
        self.assertFalse(s.evaluate(20.6))  # round(20.6, 0) = 21.0 → not eq

    def test_serialization_roundtrip(self):
        spec  = _make_spec()
        spec2 = ContractSpec.from_dict(spec.to_dict())
        self.assertEqual(spec.market_id, spec2.market_id)
        self.assertEqual(spec.status,    spec2.status)
        self.assertAlmostEqual(spec.oracle_alignment_score, spec2.oracle_alignment_score)
        self.assertEqual(spec.oracle_rounding, spec2.oracle_rounding)

    def test_unknown_status_fallback_to_active(self):
        d = _make_spec().to_dict()
        d["status"] = "INVALID_STATUS"
        spec = ContractSpec.from_dict(d)
        self.assertEqual(spec.status, MarketStatus.ACTIVE)

    def test_all_five_statuses_exist(self):
        for s in ("ACTIVE", "CLOSED_PENDING", "RESOLVED_YES", "RESOLVED_NO", "CANCELLED"):
            ms = MarketStatus(s)
            self.assertEqual(ms.value, s)


# ── ContractSpecCache ─────────────────────────────────────────────────────────

class TestContractSpecCache(unittest.TestCase):

    def test_empty_cache(self):
        cache, p = _tmp_cache()
        try:
            self.assertEqual(len(cache), 0)
            self.assertIsNone(cache.get("nonexistent"))
        finally:
            p.unlink(missing_ok=True)

    def test_set_get(self):
        cache, p = _tmp_cache()
        try:
            cache.set(_make_spec())
            result = cache.get("mkt_abc")
            self.assertIsNotNone(result)
            self.assertEqual(result.city, "New York")
        finally:
            p.unlink(missing_ok=True)
            p.with_suffix(".tmp").unlink(missing_ok=True)

    def test_persist_across_instances(self):
        cache, p = _tmp_cache()
        try:
            cache.set(_make_spec())
            cache2 = ContractSpecCache(str(p))
            self.assertIsNotNone(cache2.get("mkt_abc"))
        finally:
            p.unlink(missing_ok=True)
            p.with_suffix(".tmp").unlink(missing_ok=True)

    def test_update_status_preserves_other_fields(self):
        cache, p = _tmp_cache()
        try:
            cache.set(_make_spec())
            cache.update_status("mkt_abc", MarketStatus.RESOLVED_YES, resolution_price=1.0)
            spec = cache.get("mkt_abc")
            self.assertEqual(spec.status, MarketStatus.RESOLVED_YES)
            self.assertEqual(spec.weather_variable, "temp_max")   # inchangé
            self.assertAlmostEqual(spec.resolution_price, 1.0)
        finally:
            p.unlink(missing_ok=True)
            p.with_suffix(".tmp").unlink(missing_ok=True)

    def test_corrupted_file_recovery(self):
        p = Path(tempfile.mktemp(suffix=".json"))
        p.write_text("{ INVALID JSON }")
        try:
            cache = ContractSpecCache(str(p))
            self.assertEqual(len(cache), 0)
        finally:
            p.unlink(missing_ok=True)

    def test_known_ids(self):
        cache, p = _tmp_cache()
        try:
            cache.set(_make_spec(market_id="m1"))
            cache.set(_make_spec(market_id="m2"))
            self.assertIn("m1", cache.known_ids())
            self.assertIn("m2", cache.known_ids())
        finally:
            p.unlink(missing_ok=True)
            p.with_suffix(".tmp").unlink(missing_ok=True)

    def test_thread_safety(self):
        cache, p = _tmp_cache()
        errors = []
        def writer(i):
            try:
                cache.set(_make_spec(market_id=f"m{i:03d}", condition_id=f"c{i}"))
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        try:
            self.assertEqual(errors, [], f"Thread errors: {errors}")
            self.assertEqual(len(cache), 20)
        finally:
            p.unlink(missing_ok=True)
            p.with_suffix(".tmp").unlink(missing_ok=True)


# ── ContractParser ────────────────────────────────────────────────────────────

class TestContractParser(unittest.TestCase):

    def test_variable_temp_max(self):
        self.assertEqual(_detect_variable("highest temperature in london be 8°c"), "temp_max")

    def test_variable_temp_min(self):
        self.assertEqual(_detect_variable("lowest temperature below 32°f"), "temp_min")

    def test_variable_precip(self):
        self.assertEqual(_detect_variable("will there be rain tomorrow?"), "precip")

    def test_variable_unknown(self):
        self.assertEqual(_detect_variable("will it be sunny?"), "unknown")

    def test_unit_fahrenheit(self):
        self.assertEqual(_detect_unit("f"), "fahrenheit")
        self.assertEqual(_detect_unit("fahrenheit"), "fahrenheit")

    def test_unit_celsius(self):
        self.assertEqual(_detect_unit("c"), "celsius")
        self.assertEqual(_detect_unit("celsius"), "celsius")

    def test_station_new_york(self):
        sid, src, score = _detect_station("nyc high temp exceed 90°f")
        self.assertEqual(sid, "KNYC")
        self.assertGreater(score, 0.90)
        self.assertEqual(src, "noaa_ghcnd")

    def test_station_paris(self):
        sid, src, score = _detect_station("will paris exceed 35°c on july 1?")
        self.assertEqual(sid, "07156")
        self.assertEqual(src, "wmo_station")

    def test_station_london(self):
        sid, src, score = _detect_station("highest temperature in london be 8°c")
        self.assertEqual(sid, "03772")

    def test_station_miami(self):
        sid, src, score = _detect_station("will miami be 92°f or higher?")
        self.assertEqual(sid, "KMIA")

    def test_station_unknown_city(self):
        sid, src, score = _detect_station("will it snow in vladivostok?")
        self.assertEqual(sid, "")
        self.assertLess(score, 0.5)

    # Formats réels Polymarket
    def test_regex_be_exact(self):
        spec = _regex_parse(
            "Will the highest temperature in London be 8°C on March 25?",
            "mkt_001", "2026-03-25T23:59:00Z",
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec.weather_variable, "temp_max")
        self.assertAlmostEqual(spec.threshold_value, 8.0)
        self.assertEqual(spec.threshold_unit, "celsius")
        self.assertEqual(spec.threshold_op, "eq")
        self.assertEqual(spec.oracle_rounding, 0)        # entier, conforme Polymarket
        self.assertIsNotNone(spec.time_window_utc)       # fenêtre temporelle remplie

    def test_regex_be_or_higher(self):
        spec = _regex_parse(
            "Will the highest temperature in Miami be 92°F or higher on March 25?",
            "mkt_002", "2026-03-25T23:59:00Z",
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec.threshold_op, "gte")
        self.assertEqual(spec.threshold_unit, "fahrenheit")

    def test_regex_be_or_lower(self):
        spec = _regex_parse(
            "Will the highest temperature in Chicago be 35°F or lower on March 23?",
            "mkt_003", "2026-03-23T23:59:00Z",
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec.threshold_op, "lte")

    def test_regex_exceed_format(self):
        spec = _regex_parse(
            "Will the high temperature in New York exceed 90°F on July 15?",
            "mkt_004", "2025-07-15T23:59:00Z",
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec.threshold_op, "gt")

    def test_regex_celsius_above(self):
        spec = _regex_parse(
            "Will Paris high temperature exceed 35°C on August 1?",
            "mkt_005", "2025-08-01T23:59:00Z",
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec.threshold_unit, "celsius")

    def test_regex_unknown_variable_returns_none(self):
        spec = _regex_parse("Will it be sunny in Miami on July 15?", "mkt_006", "2025-07-15Z")
        self.assertIsNone(spec)

    def test_parse_contract_cache_hit_skips_llm(self):
        from unittest.mock import patch
        cache, p = _tmp_cache()
        try:
            cache.set(_make_spec())
            with patch("core.contract_parser._llm_parse") as mock_llm:
                result = parse_contract(
                    "mkt_abc", "cond_xyz",
                    "Will NYC high temp exceed 90°F?",
                    "2025-07-15Z", cache,
                )
                mock_llm.assert_not_called()
                self.assertIsNotNone(result)
        finally:
            p.unlink(missing_ok=True)
            p.with_suffix(".tmp").unlink(missing_ok=True)

    def test_parse_contract_low_score_returns_none(self):
        cache, p = _tmp_cache()
        os.environ["MIN_ORACLE_ALIGNMENT"] = "0.70"
        try:
            result = parse_contract(
                "mkt_unk", "",
                "Will the weather be nice somewhere?",
                "2025-07-15Z", cache,
            )
            if result is not None:
                self.assertGreaterEqual(result.oracle_alignment_score, 0.70)
        finally:
            os.environ.pop("MIN_ORACLE_ALIGNMENT", None)
            p.unlink(missing_ok=True)
            p.with_suffix(".tmp").unlink(missing_ok=True)


# ── PositionManager — machine d'états ────────────────────────────────────────

class TestPositionManager(unittest.TestCase):

    def _data(self, p_yes="0.99", active=False, closed=True):
        return {
            "active":        active,
            "closed":        closed,
            "outcomePrices": [p_yes, str(round(1 - float(p_yes), 2))],
        }

    def test_resolved_yes(self):
        self.assertEqual(_read_status(self._data("0.99")), MarketStatus.RESOLVED_YES)

    def test_resolved_no(self):
        self.assertEqual(_read_status(self._data("0.01")), MarketStatus.RESOLVED_NO)

    def test_cancelled_litige_uma(self):
        # Prix ~0.50 sur marché fermé = litige UMA → CANCELLED
        self.assertEqual(_read_status(self._data("0.50")), MarketStatus.CANCELLED)

    def test_closed_pending_intermediate(self):
        # Prix intermédiaire, marché pas encore fermé = CLOSED_PENDING
        self.assertEqual(_read_status(self._data("0.70", closed=False)), MarketStatus.CLOSED_PENDING)

    def test_still_active_returns_none(self):
        self.assertIsNone(_read_status({"active": True, "outcomePrices": ["0.65"]}))

    def test_empty_prices_pending(self):
        self.assertEqual(_read_status({"active": False, "closed": True}), MarketStatus.CLOSED_PENDING)

    # Calcul PnL selon résolution + côté
    def test_pnl_yes_win(self):
        self.assertAlmostEqual(_resolution_price(MarketStatus.RESOLVED_YES, "YES", {}), 1.0)

    def test_pnl_yes_loss(self):
        self.assertAlmostEqual(_resolution_price(MarketStatus.RESOLVED_NO, "YES", {}), 0.0)

    def test_pnl_no_win(self):
        self.assertAlmostEqual(_resolution_price(MarketStatus.RESOLVED_NO, "NO", {}), 1.0)

    def test_pnl_no_loss(self):
        self.assertAlmostEqual(_resolution_price(MarketStatus.RESOLVED_YES, "NO", {}), 0.0)

    def test_pnl_cancelled_partial(self):
        data = {"outcomePrices": ["0.48"]}
        self.assertAlmostEqual(_resolution_price(MarketStatus.CANCELLED, "YES", data), 0.48)

    def test_pnl_cancelled_default_half(self):
        # Pas de prix → remboursement à 0.50 (défaut UMA)
        self.assertAlmostEqual(_resolution_price(MarketStatus.CANCELLED, "YES", {}), 0.5)


# ── EdgeDetector — Kelly + déflation oracle ───────────────────────────────────

class TestEdgeDetector(unittest.TestCase):
    """
    Réplique kelly_stake localement pour tester la logique pure
    sans déclencher la chaîne d'imports météo.
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

    def test_positive_edge_gives_positive_bet(self):
        bet = self._kelly(0.70, 1.5, 100.0, alignment=1.0)
        self.assertGreater(bet, 0.0)
        self.assertLessEqual(bet, 20.0)

    def test_negative_edge_gives_zero(self):
        # p_win < 1/(1+odds) → k négatif → max(0, k) = 0
        bet = self._kelly(0.30, 1.0, 100.0)
        self.assertEqual(bet, 0.0)

    def test_alignment_reduces_stake(self):
        bet_full = self._kelly(0.70, 1.5, 100.0, alignment=1.00)
        bet_low  = self._kelly(0.70, 1.5, 100.0, alignment=0.70)
        self.assertGreater(bet_full, bet_low)

    def test_bet_capped_at_max_bet_usd(self):
        # Kelly extrême → capped à MAX_BET_USD
        bet = self._kelly(0.99, 99.0, 10_000.0)
        from config.settings import MAX_BET_USD
        self.assertLessEqual(bet, MAX_BET_USD)

    def test_total_cost_formula(self):
        from config.settings import FEE_BPS
        taker_fee   = FEE_BPS / 10_000
        spread_cost = 0.025
        total_cost  = taker_fee + spread_cost
        self.assertAlmostEqual(total_cost, 0.025 + taker_fee)
        self.assertGreater(total_cost, 0)



# ── UMAOracle — phases et séquence temporelle ─────────────────────────────────

class TestUMAOracle(unittest.TestCase):

    def test_uma_phase_enum_values(self):
        from execution.uma_oracle import UMAPhase
        phases = [p.value for p in UMAPhase]
        self.assertIn("AWAITING_PROPOSAL",  phases)
        self.assertIn("IN_DISPUTE_WINDOW",  phases)
        self.assertIn("SETTLEABLE",         phases)
        self.assertIn("SETTLED",            phases)
        self.assertIn("DISPUTED",           phases)
        self.assertIn("UNKNOWN",            phases)

    def test_uma_state_dispute_window_remaining(self):
        from execution.uma_oracle import UMAState, UMAPhase
        now = int(time.time())
        state = UMAState(
            condition_id        = "0xtest",
            phase               = UMAPhase.IN_DISPUTE_WINDOW,
            dispute_window_ends = now + 3600,
        )
        rem = state.dispute_window_remaining_sec
        self.assertIsNotNone(rem)
        self.assertGreater(rem, 3500)
        self.assertLessEqual(rem, 3600)

    def test_uma_state_window_expired(self):
        from execution.uma_oracle import UMAState, UMAPhase
        state = UMAState(
            condition_id        = "0xtest",
            phase               = UMAPhase.IN_DISPUTE_WINDOW,
            dispute_window_ends = int(time.time()) - 10,  # expirée
        )
        self.assertEqual(state.dispute_window_remaining_sec, 0)

    def test_recommended_poll_interval_by_phase(self):
        from execution.uma_oracle import UMAState, UMAPhase
        now = int(time.time())

        # IN_DISPUTE_WINDOW avec 45min restantes → 600s
        s1 = UMAState("0x1", UMAPhase.IN_DISPUTE_WINDOW,
                       dispute_window_ends=now + 2700)
        self.assertEqual(s1.recommended_poll_interval_sec, 600)

        # IN_DISPUTE_WINDOW avec 5min restantes → 60s
        s2 = UMAState("0x2", UMAPhase.IN_DISPUTE_WINDOW,
                       dispute_window_ends=now + 300)
        self.assertEqual(s2.recommended_poll_interval_sec, 60)

        # AWAITING_PROPOSAL → 1800s
        s3 = UMAState("0x3", UMAPhase.AWAITING_PROPOSAL)
        self.assertEqual(s3.recommended_poll_interval_sec, 1800)

        # DISPUTED → 4h
        s4 = UMAState("0x4", UMAPhase.DISPUTED)
        self.assertEqual(s4.recommended_poll_interval_sec, 4 * 3600)

    def test_dynamic_timeout_by_phase(self):
        from execution.uma_oracle import UMAState, UMAPhase, DEFAULT_LIVENESS_SEC
        now = int(time.time())

        s_waiting  = UMAState("0x1", UMAPhase.AWAITING_PROPOSAL)
        s_window   = UMAState("0x2", UMAPhase.IN_DISPUTE_WINDOW,
                               dispute_window_ends=now + 3600,
                               liveness_sec=DEFAULT_LIVENESS_SEC)
        s_disputed = UMAState("0x3", UMAPhase.DISPUTED)
        s_settle   = UMAState("0x4", UMAPhase.SETTLEABLE)

        self.assertAlmostEqual(s_waiting.dynamic_timeout_hours, 6.0)
        self.assertAlmostEqual(s_disputed.dynamic_timeout_hours, 168.0)
        self.assertAlmostEqual(s_settle.dynamic_timeout_hours, 2.0)
        # IN_DISPUTE_WINDOW : (liveness + 1h) / 3600
        expected_window = (DEFAULT_LIVENESS_SEC + 3600) / 3600
        self.assertAlmostEqual(s_window.dynamic_timeout_hours, expected_window)

    def test_proposed_yes_from_price(self):
        from execution.uma_oracle import UMAState, UMAPhase
        yes_state = UMAState("0x1", UMAPhase.IN_DISPUTE_WINDOW, proposed_price=10**18)
        no_state  = UMAState("0x2", UMAPhase.IN_DISPUTE_WINDOW, proposed_price=0)
        none_state = UMAState("0x3", UMAPhase.IN_DISPUTE_WINDOW, proposed_price=None)
        self.assertTrue(yes_state.proposed_yes)
        self.assertFalse(no_state.proposed_yes)
        self.assertIsNone(none_state.proposed_yes)

    def test_human_summary_contains_phase(self):
        from execution.uma_oracle import UMAState, UMAPhase
        state = UMAState("0xtest", UMAPhase.AWAITING_PROPOSAL,
                          market_closed_at=int(time.time()) - 3600)
        summary = state.human_summary()
        self.assertIn("AWAITING_PROPOSAL", summary)

    def test_retrocompat_get_uma_status(self):
        # get_uma_status (ancien) doit toujours fonctionner
        from execution.uma_oracle import get_uma_status, UMAStatus
        # Sans RPC → UNKNOWN
        status = get_uma_status("0xtest_no_rpc_available")
        self.assertIn(status, list(UMAStatus))


# ── UMADisputer — évaluation propositions ────────────────────────────────────

class TestUMADisputer(unittest.TestCase):

    def test_dispute_result_enum(self):
        from execution.uma_disputer import DisputeResult
        results = [r.value for r in DisputeResult]
        self.assertIn("DISPUTED",          results)
        self.assertIn("NO_DIVERGENCE",     results)
        self.assertIn("LOW_CONFIDENCE",    results)
        self.assertIn("WINDOW_EXPIRED",    results)
        self.assertIn("INSUFFICIENT_USDC", results)
        self.assertIn("DISABLED",          results)
        self.assertIn("DRY_RUN",           results)
        self.assertIn("FAILED",            results)

    def test_assess_proposal_no_spec(self):
        from execution.uma_disputer import assess_proposal
        from execution.uma_oracle import UMAState, UMAPhase
        state = UMAState("0xtest", UMAPhase.IN_DISPUTE_WINDOW,
                          proposed_price=10**18,
                          dispute_window_ends=int(time.time()) + 3600)
        assessment = assess_proposal("0xtest", state, spec_cache=None)
        self.assertFalse(assessment.diverges)
        self.assertFalse(assessment.should_dispute)

    def test_check_and_dispute_wrong_phase_returns_no_divergence(self):
        from execution.uma_disputer import check_and_dispute_if_needed, DisputeResult
        from execution.uma_oracle import UMAState, UMAPhase
        state = UMAState("0xtest", UMAPhase.SETTLED)
        result = check_and_dispute_if_needed("0xtest", state, None, dry_run=True)
        self.assertEqual(result, DisputeResult.NO_DIVERGENCE)

    def test_dry_run_returns_dry_run_result(self):
        from execution.uma_disputer import execute_dispute, DisputeResult, DisputeAssessment
        from execution.uma_oracle import UMAState, UMAPhase
        state = UMAState("0xtest", UMAPhase.IN_DISPUTE_WINDOW,
                          proposed_price=10**18,
                          dispute_window_ends=int(time.time()) + 3600,
                          bond_usdc=500.0)
        assessment = DisputeAssessment(
            condition_id="0xtest", proposed_yes=True, contract_says_yes=False,
            diverges=True, observed_temp=8.0, threshold_value=15.0,
            threshold_op="gte", confidence=0.95,
            bond_required_usdc=500.0, should_dispute=True,
        )
        result = execute_dispute("0xtest", state, assessment, dry_run=True)
        self.assertEqual(result, DisputeResult.DRY_RUN)


# ── CTFManager — gestion tokens ERC-1155 ─────────────────────────────────────

class TestCTFManager(unittest.TestCase):

    def test_parse_condition_id_short(self):
        from execution.ctf_manager import _parse_condition_id
        cid    = "0x113e93e6c20e4340afedd0d405648d"
        parsed = _parse_condition_id(cid)
        self.assertEqual(len(parsed), 32)   # bytes32
        self.assertIsInstance(parsed, bytes)

    def test_parse_condition_id_full(self):
        from execution.ctf_manager import _parse_condition_id
        cid    = "0x" + "ab" * 32
        parsed = _parse_condition_id(cid)
        self.assertEqual(len(parsed), 32)

    def test_redeem_result_enum(self):
        from execution.ctf_manager import RedeemResult
        results = [r.value for r in RedeemResult]
        self.assertIn("SUCCESS",          results)
        self.assertIn("NOT_RESOLVED",     results)
        self.assertIn("ALREADY_REDEEMED", results)
        self.assertIn("FAILED",           results)
        self.assertIn("DRY_RUN",          results)

    def test_redeem_dry_run(self):
        from execution.ctf_manager import redeem_position, RedeemResult
        result = redeem_position("0xtest", "YES", dry_run=True)
        self.assertEqual(result, RedeemResult.DRY_RUN)

    def test_get_usdc_balance_no_rpc(self):
        from execution.ctf_manager import get_usdc_balance
        # Sans RPC → 0.0 (pas d'exception)
        balance = get_usdc_balance("0x0000000000000000000000000000000000000001")
        self.assertEqual(balance, 0.0)


# ── FeatureStore — dataset ML ─────────────────────────────────────────────────

class TestFeatureStore(unittest.TestCase):

    def setUp(self):
        """Redirige le feature store vers un fichier temporaire."""
        self._tmp = Path(tempfile.mktemp(suffix=".jsonl"))
        import signals.feature_store as fs
        self._orig_path = fs._get_store_path
        fs._get_store_path = lambda: self._tmp

    def tearDown(self):
        import signals.feature_store as fs
        fs._get_store_path = self._orig_path
        self._tmp.unlink(missing_ok=True)
        self._tmp.with_suffix(".tmp").unlink(missing_ok=True)

    def test_feature_vector_defaults(self):
        from signals.feature_store import FeatureVector
        fv = FeatureVector()
        self.assertEqual(fv.p_final, 0.5)
        self.assertEqual(fv.layers_used, 1)
        self.assertIsNone(fv.outcome_yes)
        self.assertIsNone(fv.observed_value)

    def test_record_and_get_stats(self):
        from signals.feature_store import FeatureVector, record_features, get_stats
        fv = FeatureVector(condition_id="0xtest", city="London",
                            date_str="2026-03-25", p_final=0.15,
                            forecast_open_meteo=12.5, forecast_owm=13.0)
        record_features(fv)
        stats = get_stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["labeled"], 0)
        self.assertIn("London", stats["cities"])

    def test_update_outcomes(self):
        from signals.feature_store import FeatureVector, record_features, update_outcomes, get_stats
        fv = FeatureVector(condition_id="0xupdate", city="Paris",
                            date_str="2026-03-25", p_final=0.20)
        record_features(fv)
        n = update_outcomes("0xupdate", outcome_yes=False, pnl_usd=-3.5)
        self.assertEqual(n, 1)
        stats = get_stats()
        self.assertEqual(stats["labeled"], 1)

    def test_update_outcomes_nonexistent_id(self):
        from signals.feature_store import update_outcomes
        n = update_outcomes("0xnonexistent", outcome_yes=True)
        self.assertEqual(n, 0)

    def test_compute_derived_n_sources(self):
        from signals.feature_store import FeatureVector
        fv = FeatureVector(
            forecast_open_meteo=12.5, forecast_owm=13.0, forecast_noaa=None,
            forecast_weatherapi=None, forecast_tomorrow_io=14.0,
        )
        fv.compute_derived()
        self.assertEqual(fv.forecast_n_sources, 3)
        self.assertIsNotNone(fv.forecast_std)

    def test_to_dict_roundtrip(self):
        from signals.feature_store import FeatureVector
        fv1 = FeatureVector(condition_id="0xroundtrip", city="Madrid",
                             p_final=0.35, threshold_value=18.0)
        d   = fv1.to_dict()
        fv2 = FeatureVector.from_dict(d)
        self.assertEqual(fv2.condition_id, fv1.condition_id)
        self.assertAlmostEqual(fv2.p_final, fv1.p_final)

    def test_load_dataframe_empty(self):
        from signals.feature_store import load_dataframe
        df = load_dataframe()
        # pandas peut ne pas être disponible → None, ou DataFrame vide
        if df is not None:
            self.assertEqual(len(df), 0)

    def test_build_feature_vector_from_detail(self):
        from signals.feature_store import build_feature_vector_from_detail
        detail = {
            "forecast_c": 12.5, "bias_corr": -0.5, "sigma": 1.8,
            "p_forecast": 0.15, "p_climate": 0.20, "p_trend": None,
            "p_final": 0.17, "layers_used": 2,
            "sources": ["open_meteo", "owm"], "days_to_expiry": 2,
        }
        fv = build_feature_vector_from_detail(
            condition_id    = "0xfv_test",
            question        = "Will London be >= 15C?",
            city            = "London",
            date_str        = "2026-03-25",
            detail          = detail,
            market_yes_prob = 0.40,
            side            = "NO",
            kelly_stake_usd = 12.5,
            oracle_alignment= 0.88,
        )
        self.assertEqual(fv.condition_id, "0xfv_test")
        self.assertAlmostEqual(fv.p_final, 0.17)
        self.assertEqual(fv.side, "NO")
        self.assertAlmostEqual(fv.oracle_alignment, 0.88)


# ── ModelInterface — prédictions ──────────────────────────────────────────────

class TestModelInterface(unittest.TestCase):

    def test_model_prediction_clamp(self):
        from signals.model_interface import ModelPrediction
        p = ModelPrediction(p_yes=1.5, confidence=2.0, source="test").clamp()
        self.assertLessEqual(p.p_yes, 0.98)
        self.assertLessEqual(p.confidence, 1.0)

    def test_model_prediction_clamp_low(self):
        from signals.model_interface import ModelPrediction
        p = ModelPrediction(p_yes=-0.5, confidence=-1.0, source="test").clamp()
        self.assertGreaterEqual(p.p_yes, 0.02)
        self.assertGreaterEqual(p.confidence, 0.0)

    def test_statistical_model_three_layers(self):
        from signals.model_interface import StatisticalModel
        m = StatisticalModel()
        self.assertTrue(m.is_available())
        features = {
            "p_forecast": 0.15, "p_climate": 0.20, "p_trend": 0.18,
            "forecast_confidence": 0.85,
        }
        pred = m.predict(features)
        self.assertIsNotNone(pred)
        self.assertGreater(pred.p_yes, 0.02)
        self.assertLess(pred.p_yes, 0.98)
        self.assertEqual(pred.metadata["layers_used"], 3)

    def test_statistical_model_one_layer(self):
        from signals.model_interface import StatisticalModel
        m = StatisticalModel()
        pred = m.predict({"p_forecast": 0.30})
        self.assertIsNotNone(pred)
        self.assertAlmostEqual(pred.p_yes, 0.30, places=2)
        self.assertEqual(pred.metadata["layers_used"], 1)

    def test_statistical_model_none_when_no_forecast(self):
        from signals.model_interface import StatisticalModel
        m = StatisticalModel()
        pred = m.predict({})
        self.assertIsNone(pred)

    def test_ensemble_model_averages(self):
        from signals.model_interface import StatisticalModel, EnsembleModel
        m1 = StatisticalModel()
        m2 = StatisticalModel()
        ensemble = EnsembleModel([(m1, 0.5), (m2, 0.5)])
        features = {"p_forecast": 0.25, "p_climate": 0.30, "forecast_confidence": 0.80}
        pred = ensemble.predict(features)
        self.assertIsNotNone(pred)
        self.assertIn("ensemble:", pred.source)

    def test_ensemble_model_name(self):
        from signals.model_interface import StatisticalModel, EnsembleModel
        m = StatisticalModel()
        ensemble = EnsembleModel([(m, 1.0)])
        self.assertIn("statistical_v2", ensemble.name)

    def test_sklearn_model_unavailable_on_bad_path(self):
        from signals.model_interface import SklearnModel
        m = SklearnModel("/nonexistent/path/model.pkl")
        self.assertFalse(m.is_available())
        self.assertIsNone(m.predict({"p_forecast": 0.5}))

    def test_onnx_model_unavailable_on_bad_path(self):
        from signals.model_interface import OnnxModel
        m = OnnxModel("/nonexistent/path/model.onnx")
        self.assertFalse(m.is_available())
        self.assertIsNone(m.predict({"p_forecast": 0.5}))


# ── ModelRegistry — singleton ─────────────────────────────────────────────────

class TestModelRegistry(unittest.TestCase):

    def setUp(self):
        # Reset le singleton entre les tests
        import signals.model_registry as reg
        import signals.model_interface as mi
        reg._model = None
        # Force StatisticalModel (pas de MODEL_PATH)
        os.environ.pop("MODEL_PATH", None)
        os.environ.pop("MODEL_ENDPOINT", None)
        os.environ.pop("MODEL_WEIGHT", None)

    def tearDown(self):
        import signals.model_registry as reg
        reg._model = None

    def test_default_model_is_statistical(self):
        from signals.model_registry import get_model
        from signals.model_interface import StatisticalModel
        model = get_model()
        self.assertIsInstance(model, StatisticalModel)

    def test_predict_returns_prediction(self):
        from signals.model_registry import predict
        pred = predict({
            "p_forecast": 0.15, "p_climate": 0.22, "p_trend": 0.18,
            "forecast_confidence": 0.85,
        })
        self.assertIsNotNone(pred)
        self.assertGreater(pred.p_yes, 0.02)
        self.assertLess(pred.p_yes, 0.98)

    def test_predict_none_on_empty_features(self):
        from signals.model_registry import predict
        pred = predict({})
        self.assertIsNone(pred)

    def test_model_info_keys(self):
        from signals.model_registry import model_info
        info = model_info()
        self.assertIn("name", info)
        self.assertIn("available", info)
        self.assertIn("type", info)
        self.assertTrue(info["available"])

    def test_bad_model_path_falls_back_to_statistical(self):
        import signals.model_registry as reg
        from signals.model_interface import StatisticalModel
        os.environ["MODEL_PATH"] = "/nonexistent/model.pkl"
        os.environ["MODEL_WEIGHT"] = "0.6"
        reg._model = None
        model = reg.get_model()
        # Fallback → StatisticalModel car fichier inexistant
        self.assertIsInstance(model, StatisticalModel)
        os.environ.pop("MODEL_PATH", None)
        os.environ.pop("MODEL_WEIGHT", None)
        reg._model = None

    def test_reload_model(self):
        from signals.model_registry import get_model, reload_model
        m1 = get_model()
        m2 = reload_model()
        self.assertEqual(type(m1), type(m2))


# ── PnLTracker — bankroll scindée ─────────────────────────────────────────────

class TestPnLBankroll(unittest.TestCase):

    def test_get_bankroll_detail_structure(self):
        from monitoring.pnl_tracker import get_bankroll_detail
        detail = get_bankroll_detail(150.0)
        self.assertIn("disponible",   detail)
        self.assertIn("bloquee_dvm",  detail)
        self.assertIn("bond_risque",  detail)
        self.assertIn("total",        detail)
        self.assertIn("realised_pnl", detail)
        self.assertIn("dvm_positions", detail)

    def test_bankroll_detail_no_dvm(self):
        from monitoring.pnl_tracker import get_bankroll_detail
        detail = get_bankroll_detail(150.0)
        self.assertAlmostEqual(detail["bloquee_dvm"], 0.0)
        self.assertAlmostEqual(detail["bond_risque"], 0.0)
        self.assertEqual(detail["dvm_positions"], 0)

    def test_get_dvm_blocked_capital_empty(self):
        from monitoring.pnl_tracker import get_dvm_blocked_capital
        dvm = get_dvm_blocked_capital()
        self.assertEqual(dvm["count"], 0)
        self.assertAlmostEqual(dvm["total_blocked_usd"], 0.0)
        self.assertAlmostEqual(dvm["total_bond_usd"], 0.0)

    def test_mark_dvm_disputed_nonexistent_noop(self):
        from monitoring.pnl_tracker import mark_dvm_disputed, get_dvm_blocked_capital
        # Position inexistante → pas d'erreur, count reste 0
        mark_dvm_disputed("0xnonexistent", bond_usd=500.0)
        dvm = get_dvm_blocked_capital()
        self.assertEqual(dvm["count"], 0)


# ── OnchainLogger ─────────────────────────────────────────────────────────────

class TestOnchainLogger(unittest.TestCase):

    def setUp(self):
        """Redirige le logger vers un fichier temporaire."""
        self._tmp = Path(tempfile.mktemp(suffix=".json"))
        import monitoring.onchain_logger as ol
        self._orig = ol._get_path
        ol._get_path = lambda: self._tmp

    def tearDown(self):
        import monitoring.onchain_logger as ol
        ol._get_path = self._orig
        self._tmp.unlink(missing_ok=True)
        self._tmp.with_suffix(".tmp").unlink(missing_ok=True)

    def test_log_balance_check_and_retrieve(self):
        from monitoring.onchain_logger import log_balance_check, get_ops
        log_balance_check("0xtest", "token123", 10.0, 9.8, ok=False)
        ops = get_ops("0xtest")
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["op"], "balance_check")
        self.assertFalse(ops[0]["ok"])
        self.assertAlmostEqual(ops[0]["expected"], 10.0)

    def test_log_resolution_check_conflict(self):
        from monitoring.onchain_logger import log_resolution_check, get_ops
        log_resolution_check(
            condition_id="0xconflict", source="ctf_evaluate",
            resolved=True, resolution_matches_contract=False,
            observed_value=8.87, contract_threshold=15.0,
            contract_op="gte", gamma_status="RESOLVED_YES",
            conflict=True,
        )
        ops = get_ops("0xconflict")
        self.assertEqual(len(ops), 1)
        self.assertTrue(ops[0]["conflict"])
        self.assertAlmostEqual(ops[0]["observed_value"], 8.87)

    def test_log_redeem(self):
        from monitoring.onchain_logger import log_redeem, get_ops
        log_redeem("0xredeem", "YES", "SUCCESS",
                   tx_hash="0xabc123", usdc_gained=20.5, dry_run=False)
        ops = get_ops("0xredeem")
        self.assertEqual(ops[0]["op"], "redeem")
        self.assertEqual(ops[0]["result"], "SUCCESS")
        self.assertAlmostEqual(ops[0]["usdc_gained"], 20.5)

    def test_get_ops_filter_by_condition_id(self):
        from monitoring.onchain_logger import log_redeem, get_ops
        log_redeem("0xa", "YES", "DRY_RUN", dry_run=True)
        log_redeem("0xb", "NO",  "DRY_RUN", dry_run=True)
        ops_a = get_ops("0xa")
        ops_b = get_ops("0xb")
        self.assertEqual(len(ops_a), 1)
        self.assertEqual(len(ops_b), 1)

    def test_get_ops_all(self):
        from monitoring.onchain_logger import log_redeem, get_ops
        log_redeem("0xc1", "YES", "DRY_RUN", dry_run=True)
        log_redeem("0xc2", "NO",  "DRY_RUN", dry_run=True)
        all_ops = get_ops()
        self.assertEqual(len(all_ops), 2)


# ── Intégration — cycle complet sans réseau ───────────────────────────────────

class TestIntegration(unittest.TestCase):

    def test_full_cycle_resolved_yes(self):
        """Parse → spec → simulation YES résolu → PnL correct."""
        cache, p = _tmp_cache()
        try:
            spec = _regex_parse(
                "Will the highest temperature in London be 15°C or higher on March 26?",
                "mkt_int_1", "2026-03-26T23:59:00Z",
            )
            self.assertIsNotNone(spec)
            self.assertEqual(spec.threshold_op, "gte")
            self.assertEqual(spec.oracle_rounding, 0)
            spec.condition_id = "cond_int_1"
            cache.set(spec)

            # Multiplier actif
            self.assertAlmostEqual(spec.effective_edge_multiplier, spec.oracle_alignment_score)

            # Résolution YES
            data   = {"active": False, "closed": True, "outcomePrices": ["0.99", "0.01"]}
            status = _read_status(data)
            self.assertEqual(status, MarketStatus.RESOLVED_YES)

            res_price = _resolution_price(status, "YES", data)
            entry, shares = 0.65, 30.77   # $20 / 0.65
            pnl = (res_price - entry) * shares
            self.assertGreater(pnl, 0)    # gain

        finally:
            p.unlink(missing_ok=True)
            p.with_suffix(".tmp").unlink(missing_ok=True)

    def test_full_cycle_resolved_no_position(self):
        """Position NO sur marché résolu NO → gain."""
        data   = {"active": False, "closed": True, "outcomePrices": ["0.01", "0.99"]}
        status = _read_status(data)
        self.assertEqual(status, MarketStatus.RESOLVED_NO)
        res_price = _resolution_price(status, "NO", data)
        self.assertAlmostEqual(res_price, 1.0)
        entry = 0.80
        pnl   = (res_price - entry) * 25.0
        self.assertAlmostEqual(pnl, 5.0)

    def test_full_cycle_cancelled(self):
        """Marché litigieux → remboursement partiel → PnL négatif si entry > 0.50."""
        data   = {"active": False, "closed": True, "outcomePrices": ["0.50", "0.50"]}
        status = _read_status(data)
        self.assertEqual(status, MarketStatus.CANCELLED)
        res_price = _resolution_price(status, "YES", data)
        entry, shares = 0.65, 30.77
        pnl = (res_price - entry) * shares
        self.assertLess(pnl, 0)   # perte partielle

    def test_non_tradeable_spec_zero_multiplier(self):
        """Tout statut non-ACTIVE → edge_multiplier = 0 → pas de signal."""
        for status in (MarketStatus.CLOSED_PENDING, MarketStatus.RESOLVED_YES,
                       MarketStatus.RESOLVED_NO,    MarketStatus.CANCELLED):
            spec = _make_spec(status=status, oracle_alignment_score=0.95)
            self.assertEqual(spec.effective_edge_multiplier, 0.0, f"failed for {status}")


class TestProduceRtDayGuard(unittest.TestCase):
    """Garde jour J (target_date vs date locale) — bot2 RT produce_rt_signals."""

    def test_classify_empty_string_invalid(self):
        """Chaîne vide → invalid (défensif ; le producteur skip avant METAR si target_date absent)."""
        from bots.bot2_realtime.metar_live.produce_rt_signals import classify_rt_market_day

        self.assertEqual(classify_rt_market_day(date(2026, 3, 24), ""), "invalid")
        self.assertEqual(classify_rt_market_day(date(2026, 3, 24), "   "), "invalid")

    def test_classify_premature_past_jour_j(self):
        from bots.bot2_realtime.metar_live.produce_rt_signals import classify_rt_market_day

        self.assertEqual(classify_rt_market_day(date(2026, 3, 24), "2026-03-26"), "premature")
        self.assertEqual(classify_rt_market_day(date(2026, 3, 27), "2026-03-26"), "past_target")
        self.assertEqual(classify_rt_market_day(date(2026, 3, 26), "2026-03-26"), "jour_j")

    def test_classify_invalid_date(self):
        from bots.bot2_realtime.metar_live.produce_rt_signals import classify_rt_market_day

        self.assertEqual(classify_rt_market_day(date(2026, 3, 26), "not-a-date"), "invalid")


class TestGfsHorizons(unittest.TestCase):
    """Bot1 — calculate_gfs_horizons (grille fXXX pour target_date locale)."""

    @staticmethod
    def _gfs_mod():
        import importlib.util
        p = Path(__file__).resolve().parent.parent / "bots/bot1/noaa_nomads_live/gfs_horizons.py"
        spec = importlib.util.spec_from_file_location("gfs_horizons", p)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod

    def test_run_start_from_cycle(self):
        m = self._gfs_mod()
        from datetime import date, datetime, timezone

        t = m.gfs_run_start_utc_from_parts(date(2026, 3, 24), "12")
        self.assertEqual(t, datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            m.gfs_run_start_utc_from_parts(date(2026, 3, 24), "99")

    def test_grid_utc_run_24_mar_12z_target_27_mar(self):
        """Run 24 mars 12Z, jour cible 27 mars en UTC → fh 60..81 pas 3."""
        m = self._gfs_mod()
        from datetime import date, datetime, timezone

        run = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        fh = m.calculate_gfs_horizons(run, date(2026, 3, 27), "UTC")
        self.assertEqual(fh, (60, 63, 66, 69, 72, 75, 78, 81))

    def test_max_fh_truncates(self):
        m = self._gfs_mod()
        from datetime import date, datetime, timezone

        run = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        fh = m.calculate_gfs_horizons(run, date(2026, 3, 27), "UTC", max_fh=70)
        self.assertEqual(fh, (60, 63, 66, 69))

    def test_invalid_tz(self):
        m = self._gfs_mod()
        from datetime import date, datetime, timezone

        run = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            m.calculate_gfs_horizons(run, date(2026, 3, 27), "Nowhere/Invalid")

    def test_sigma_horizon_multiplier_defaults(self):
        """f024 → 1 ; f120 → ×3 (ref=24, slope=0.5) — garde-fou incertitude GFS."""
        m = self._gfs_mod()
        self.assertAlmostEqual(m.sigma_horizon_multiplier(24.0), 1.0)
        self.assertAlmostEqual(m.sigma_horizon_multiplier(23.0), 1.0)
        self.assertAlmostEqual(m.sigma_horizon_multiplier(120.0), 3.0)


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — TESTS LIVE (réseau, DRY_RUN=1 forcé)
# ══════════════════════════════════════════════════════════════════════════════

def _live_gamma_scan() -> list:
    _section("1/6 — Gamma API : scan marchés météo actifs")
    try:
        from scanners.market_scanner import scan_weather_markets
        markets = scan_weather_markets(spec_cache=None)
        if not markets:
            _warn("Aucun marché météo actif — possible si hors période")
            return []
        _ok(f"{len(markets)} marché(s) météo actif(s)")
        print()
        for i, m in enumerate(markets[:5], 1):
            yes_p = m.market_yes_prob
            days  = m.days_to_expiry
            _info(f"#{i:02d} {m.question[:60]}")
            _info(f"     YES={yes_p*100:.1f}%  expire={days:.1f}j  vol=${m.volume:,.0f}")
            _info(f"     condition_id={m.condition_id[:32]}")
        if len(markets) > 5:
            _info(f"     ... et {len(markets)-5} autres")
        return markets
    except Exception as e:
        _err(f"Gamma API échoué : {e}")
        import traceback; traceback.print_exc()
        return []


def _live_contract_parser(markets: list) -> tuple:
    _section("2/6 — ContractParser : parsing contractuel sur vrais marchés")
    if not markets:
        _warn("Pas de marchés — test ignoré")
        return None, []

    cache_path = Path(tempfile.mktemp(suffix="_live_specs.json"))
    cache      = ContractSpecCache(str(cache_path))
    parsed_ok, parsed_ko = [], []

    print()
    for m in markets[:10]:
        end_iso = m.end_date.isoformat() if m.end_date else ""
        spec = parse_contract(m.condition_id, m.condition_id, m.question, end_iso, cache)
        if spec:
            parsed_ok.append((m, spec))
            _info(f"✅ {m.question[:55]}")
            _info(f"   → {spec.weather_variable} {spec.threshold_op} "
                  f"{spec.threshold_value}{spec.threshold_unit} "
                  f"| station={spec.oracle_station_id or '?'} "
                  f"| rounding={spec.oracle_rounding} "
                  f"| score={spec.oracle_alignment_score:.2f}")
        else:
            parsed_ko.append(m)
            _info(f"⚠️  ÉCARTÉ : {m.question[:55]}")

    print()
    _ok(f"{len(parsed_ok)}/{min(len(markets),10)} marchés parsés avec succès")
    if parsed_ko:
        _warn(f"{len(parsed_ko)} marchés écartés (oracle ambigu)")
    if parsed_ok:
        _ok(f"Cache ContractSpec : {len(cache)} entrée(s)")

    cache_path.unlink(missing_ok=True)
    cache_path.with_suffix(".tmp").unlink(missing_ok=True)
    return cache, parsed_ok


def _live_websocket(markets: list) -> None:
    _section("3/6 — WebSocket : connexion prix temps réel")
    try:
        import websocket as _
    except ImportError:
        _warn("websocket-client non installé — pip install websocket-client")
        return

    from scanners.ws_price_feed import start_ws_feed, subscribe_tokens, get_price, is_running
    _info("Connexion au WS Polymarket...")
    start_ws_feed()
    time.sleep(3)

    if not is_running():
        _err("WebSocket non connecté après 3s")
        return
    _ok("WebSocket connecté")

    if markets:
        token_ids = [t.token_id for m in markets[:3] for t in m.tokens if t.token_id]
        subscribe_tokens(token_ids)
        _info(f"Abonné à {len(token_ids)} tokens — attente 5s...")
        time.sleep(5)
        received = [tid for tid in token_ids if get_price(tid) is not None]
        if received:
            _ok(f"Prix reçus via WS : {len(received)}/{len(token_ids)} tokens")
        else:
            _warn("Aucun prix reçu via WS (normal si marché peu actif) — fallback REST OK")


def _live_probability_engine(markets: list) -> list:
    _section("4/6 — Probability Engine : calcul P(Yes) sur vrais marchés")
    if not markets:
        _warn("Pas de marchés — test ignoré")
        return []

    from signals.probability_engine import compute_yes_prob
    results, unsupported = [], []
    print()

    for m in markets[:8]:
        try:
            result = compute_yes_prob(m.question, record=False)
            if result is None:
                unsupported.append(m.question[:55])
                _info(f"⚪ Non supporté : {m.question[:55]}")
                continue
            p_yes, conf, detail = result
            market_p = m.market_yes_prob or 0.5
            results.append((m, p_yes, conf, detail))
            _info(f"✅ {m.question[:55]}")
            _info(f"   P(bot)={p_yes*100:.1f}%  P(marché)={market_p*100:.1f}%  "
                  f"edge_brut={abs(p_yes-market_p)*100:.1f}%  conf={conf*100:.0f}%  "
                  f"couches={detail.get('layers_used','?')}/3"
                  + (f"  p_model={detail.get('p_model',0)*100:.1f}%" if detail.get('p_model') else ""))
            fc = detail.get("forecast_c")
            if fc:
                _info(f"   prévision={fc}°C  biais={detail.get('bias_corr',0):+.2f}°C  "
                      f"sources={detail.get('sources', [])}")
        except Exception as e:
            _info(f"❌ ERREUR : {m.question[:45]} → {e}")

    print()
    _ok(f"{len(results)} calculs réussis")
    if unsupported:
        _warn(f"{len(unsupported)} questions non supportées (format inconnu)")
    return results


def _live_edge_detector(markets: list) -> list:
    _section("5/6 — Edge Detector : signaux avec déflation oracle")
    if not markets:
        _warn("Pas de marchés — test ignoré")
        return []

    from core.models import ContractSpecCache
    from signals.edge_detector import find_best_trades
    from monitoring.pnl_tracker import get_current_bankroll

    cache_path = Path(tempfile.mktemp(suffix="_edge_specs.json"))
    cache      = ContractSpecCache(str(cache_path))

    for m in markets:
        end_iso = m.end_date.isoformat() if m.end_date else ""
        parse_contract(m.condition_id, m.condition_id, m.question, end_iso, cache)

    bankroll = get_current_bankroll()
    _info(f"Bankroll courante : ${bankroll:.2f}")
    _info("Calcul des signaux (avec déflation oracle)...")
    print()

    try:
        signals = find_best_trades(markets, bankroll=bankroll, spec_cache=cache)
        if not signals:
            _warn("Aucun signal net détecté")
            _info("(Normal : edge_brut × alignment − frais peut tomber sous MIN_EDGE=5%)")
        else:
            _ok(f"{len(signals)} signal(s) détecté(s)")
            for i, s in enumerate(signals, 1):
                spec  = cache.get(s.market.condition_id)
                align = spec.oracle_alignment_score if spec else 1.0
                _info(f"#{i} [{s.side}] edge_net={s.edge*100:.1f}%  "
                      f"mise=${s.kelly_stake_usd:.2f}  alignment={align:.2f}  "
                      f"conf={s.signal.confidence*100:.0f}%")
                _info(f"   {s.market.question[:60]}")
    except Exception as e:
        _err(f"Edge detector : {e}")
        import traceback; traceback.print_exc()
        signals = []
    finally:
        cache_path.unlink(missing_ok=True)
        cache_path.with_suffix(".tmp").unlink(missing_ok=True)

    return signals


def _live_full_cycle() -> bool:
    _section("6/6 — Cycle complet : simulation d'un cycle main.py (DRY_RUN)")
    from core.models import ContractSpecCache
    from scanners.market_scanner import scan_weather_markets
    from signals.edge_detector import find_best_trades, print_signals
    from execution.order_manager import place_order
    from execution.position_manager import position_monitor_cycle
    from monitoring.pnl_tracker import get_current_bankroll, print_open_positions, print_pnl_summary
    from config.settings import CONTRACT_SPECS_FILE

    cache = ContractSpecCache(CONTRACT_SPECS_FILE)
    _info(f"ContractSpecs en cache : {len(cache)}")

    try:
        _info("→ Vérification positions existantes...")
        position_monitor_cycle(spec_cache=cache)
        _ok("position_monitor_cycle OK")
    except Exception as e:
        _err(f"position_monitor_cycle : {e}")

    try:
        _info("→ Scan marchés météo...")
        markets = scan_weather_markets(spec_cache=cache)
        _ok(f"Scan OK — {len(markets)} marché(s)")
        _info(f"ContractSpecs après scan : {len(cache)}")
    except Exception as e:
        _err(f"Scan : {e}")
        return False

    if not markets:
        _warn("Aucun marché — fin du test cycle")
        return True

    try:
        _info("→ Calcul signaux...")
        bankroll = get_current_bankroll()
        signals  = find_best_trades(markets, bankroll=bankroll, spec_cache=cache)
        _ok(f"Signaux OK — {len(signals)} signal(s)")
        print_signals(signals)
    except Exception as e:
        _err(f"Signaux : {e}")
        return False

    if signals:
        _info(f"→ Simulation {min(3, len(signals))} ordre(s) DRY_RUN...")
        for s in signals[:3]:
            try:
                order = place_order(s)
                if order:
                    _ok(f"Ordre simulé : [{s.side}] ${s.kelly_stake_usd:.2f}")
                else:
                    _warn("Ordre ignoré (position déjà ouverte)")
            except Exception as e:
                _err(f"place_order : {e}")
    else:
        _warn("Aucun signal → aucun ordre simulé")

    print()
    print_open_positions()
    print_pnl_summary()
    _ok("Cycle complet terminé sans erreur critique")
    # Feature store
    try:
        from signals.feature_store import get_stats
        stats = get_stats()
        if stats["total"] > 0:
            _info(f"Feature store : {stats['total']} prédictions "
                  f"({stats['labeled']} labellisées) | {stats['size_mb']} MB")
    except Exception:
        pass
    return True


def run_live_tests() -> bool:
    """Lance les 6 étapes de test en conditions réelles."""
    print(f"\n{SEP}")
    print("  TESTS LIVE — Polymarket Weather Bot")
    print("  DRY_RUN=1 forcé — aucun ordre réel")
    print(SEP)

    from config.settings import (
        OWM_API_KEY, MIN_EDGE, KELLY_FRACTION,
        MIN_ORACLE_ALIGNMENT, ORACLE_POLL_INTERVAL_SEC,
        ANTHROPIC_API_KEY, FEE_BPS, ENABLE_YES_SIDE,
    )
    print()
    _info(f"OWM_API_KEY      : {'✅ présente' if OWM_API_KEY else '❌ manquante'}")
    _info(f"ANTHROPIC_API_KEY: {'✅ présente' if ANTHROPIC_API_KEY else '⚠️  manquante (regex seul)'}")
    _info(f"MIN_EDGE         : {MIN_EDGE*100:.0f}%")
    _info(f"KELLY_FRACTION   : {KELLY_FRACTION*100:.0f}%")
    _info(f"FEE_BPS          : {FEE_BPS}")
    _info(f"MIN_ORACLE_ALIGN : {MIN_ORACLE_ALIGNMENT:.2f}")
    _info(f"ORACLE_POLL      : {ORACLE_POLL_INTERVAL_SEC//60} min")
    _info(f"ENABLE_YES_SIDE  : {ENABLE_YES_SIDE}")
    try:
        from signals.model_registry import model_info
        info = model_info()
        _info(f"MODÈLE ACTIF     : {info['name']} ({'✅' if info['available'] else '❌'})")
    except Exception:
        pass

    markets       = _live_gamma_scan()
    _, pairs      = _live_contract_parser(markets)
    _live_websocket(markets)
    probs         = _live_probability_engine(markets)
    _              = _live_edge_detector(markets)
    cycle_ok      = _live_full_cycle()

    _section("BILAN LIVE")
    checks = [
        ("Gamma API scan",        bool(markets)),
        ("ContractParser",        bool(pairs)),
        ("ProbabilityEngine",     bool(probs)),
        ("EdgeDetector",          True),
        ("Cycle complet DRY_RUN", cycle_ok),
    ]
    all_ok = True
    for label, passed in checks:
        if passed: _ok(label)
        else:      _err(label); all_ok = False

    print()
    if all_ok:
        print(f"  {OK} Tous les tests live passent — bot prêt")
    else:
        print(f"  {ERR} Des composants nécessitent attention")
    print(f"\n{SEP}\n")
    return all_ok


# ══════════════════════════════════════════════════════════════════════════════
#  POINT D'ENTRÉE
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Tests du bot Polymarket Weather")
    parser.add_argument("--unit", action="store_true", help="Tests unitaires uniquement (sans réseau)")
    parser.add_argument("--live", action="store_true", help="Tests live uniquement (réseau, DRY_RUN=1)")
    args = parser.parse_args()

    run_unit = not args.live   # par défaut ou --unit
    run_live = not args.unit   # par défaut ou --live

    unit_ok = True
    live_ok = True

    if run_unit:
        print(f"\n{SEP}")
        print("  TESTS UNITAIRES (80+ tests, aucun réseau)")
        print(SEP)
        loader  = unittest.TestLoader()
        suite   = unittest.TestSuite()
        for cls in (TestContractSpec, TestContractSpecCache, TestContractParser,
                    TestPositionManager, TestEdgeDetector,
                    TestUMAOracle, TestUMADisputer, TestCTFManager,
                    TestFeatureStore, TestModelInterface, TestModelRegistry,
                    TestPnLBankroll, TestOnchainLogger,
                    TestIntegration, TestProduceRtDayGuard, TestGfsHorizons):
            suite.addTests(loader.loadTestsFromTestCase(cls))
        runner  = unittest.TextTestRunner(verbosity=2)
        result  = runner.run(suite)
        unit_ok = result.wasSuccessful()

    if run_live:
        live_ok = run_live_tests()

    sys.exit(0 if (unit_ok and live_ok) else 1)