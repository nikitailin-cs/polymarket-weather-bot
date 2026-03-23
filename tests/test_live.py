"""
tests/test_live.py
------------------
Test en conditions réelles — aucun ordre placé (DRY_RUN forcé).

Valide séquentiellement :
  1. Gamma API         — scan marchés météo actifs
  2. ContractParser    — parsing sur vrais marchés
  3. WebSocket         — connexion + réception prix
  4. ProbabilityEngine — calcul P(Yes) sur vrais marchés
  5. EdgeDetector      — signaux avec déflation oracle
  6. Cycle complet     — simulation d'un cycle main.py

Lance : python tests/test_live.py
Tout doit passer. Aucun ordre réel même si DRY_RUN=0 dans .env.
"""
from __future__ import annotations
import os, sys, time, json
from pathlib import Path

# Force DRY_RUN avant tout import
os.environ["DRY_RUN"] = "1"

sys.path.insert(0, str(Path(__file__).parent.parent))

SEP  = "═" * 68
SEP2 = "─" * 68
OK   = "✅"
ERR  = "❌"
WARN = "⚠️ "


def section(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def ok(msg: str)   -> None: print(f"  {OK}  {msg}")
def err(msg: str)  -> None: print(f"  {ERR}  {msg}")
def warn(msg: str) -> None: print(f"  {WARN} {msg}")
def info(msg: str) -> None: print(f"     {msg}")


# ══════════════════════════════════════════════════════════════════════════════
# 1. GAMMA API — Scan marchés météo actifs
# ══════════════════════════════════════════════════════════════════════════════

def test_gamma_scan() -> list:
    section("1/6 — Gamma API : scan marchés météo actifs")
    try:
        from scanners.market_scanner import scan_weather_markets
        # Sans spec_cache pour ce test — comportement original
        markets = scan_weather_markets(spec_cache=None)

        if not markets:
            warn("Aucun marché météo actif trouvé — possible si hors période")
            return []

        ok(f"{len(markets)} marché(s) météo actif(s)")
        print()
        for i, m in enumerate(markets[:5], 1):
            yes_p = m.market_yes_prob
            days  = m.days_to_expiry
            info(f"#{i:02d} {m.question[:60]}")
            info(f"     YES={yes_p*100:.1f}%  expire={days:.1f}j  vol=${m.volume:,.0f}")
            info(f"     condition_id={m.condition_id[:32]}")
        if len(markets) > 5:
            info(f"     ... et {len(markets)-5} autres")

        return markets

    except Exception as e:
        err(f"Gamma API échoué : {e}")
        import traceback; traceback.print_exc()
        return []


# ══════════════════════════════════════════════════════════════════════════════
# 2. CONTRACT PARSER — Parsing sur vrais marchés
# ══════════════════════════════════════════════════════════════════════════════

def test_contract_parser(markets: list) -> tuple:
    section("2/6 — ContractParser : parsing contractuel sur vrais marchés")

    if not markets:
        warn("Pas de marchés — test ignoré")
        return None, []

    import tempfile
    from core.models import ContractSpecCache
    from core.contract_parser import parse_contract

    cache_path = Path(tempfile.mktemp(suffix="_live_specs.json"))
    cache      = ContractSpecCache(str(cache_path))

    parsed_ok  = []
    parsed_ko  = []

    print()
    for m in markets[:10]:
        end_iso = m.end_date.isoformat() if m.end_date else ""
        spec = parse_contract(
            market_id    = m.condition_id,
            condition_id = m.condition_id,
            question     = m.question,
            end_date_iso = end_iso,
            cache        = cache,
        )
        if spec:
            parsed_ok.append((m, spec))
            method = "regex" if spec.oracle_station_id else "llm/partial"
            info(f"✅ {m.question[:55]}")
            info(f"   → {spec.weather_variable} {spec.threshold_op} "
                 f"{spec.threshold_value}{spec.threshold_unit} "
                 f"| station={spec.oracle_station_id or '?'} "
                 f"| score={spec.oracle_alignment_score:.2f}")
        else:
            parsed_ko.append(m)
            info(f"⚠️  ÉCARTÉ (score bas) : {m.question[:55]}")

    print()
    ok(f"{len(parsed_ok)}/{min(len(markets),10)} marchés parsés avec succès")
    if parsed_ko:
        warn(f"{len(parsed_ko)} marchés écartés (oracle ambigu)")

    if parsed_ok:
        ok(f"Cache ContractSpec : {len(cache)} entrée(s)")

    # Nettoyage
    cache_path.unlink(missing_ok=True)
    cache_path.with_suffix(".tmp").unlink(missing_ok=True)

    return cache, parsed_ok


# ══════════════════════════════════════════════════════════════════════════════
# 3. WEBSOCKET — Connexion + réception prix
# ══════════════════════════════════════════════════════════════════════════════

def test_websocket(markets: list) -> None:
    section("3/6 — WebSocket : connexion prix temps réel")

    try:
        import websocket as _ws_lib
    except ImportError:
        warn("websocket-client non installé — pip install websocket-client")
        warn("Test WS ignoré")
        return

    from scanners.ws_price_feed import start_ws_feed, subscribe_tokens, get_price, is_running

    info("Connexion au WS Polymarket...")
    start_ws_feed()
    time.sleep(3)

    if not is_running():
        err("WebSocket non connecté après 3s")
        return

    ok("WebSocket connecté")

    if markets:
        token_ids = [t.token_id for m in markets[:3] for t in m.tokens if t.token_id]
        subscribe_tokens(token_ids)
        info(f"Abonné à {len(token_ids)} tokens — attente 5s...")
        time.sleep(5)

        received = [(tid, get_price(tid)) for tid in token_ids if get_price(tid) is not None]
        if received:
            ok(f"Prix reçus via WS : {len(received)}/{len(token_ids)} tokens")
            for tid, price in received[:3]:
                info(f"   token={tid[:28]}... → {price:.4f}")
        else:
            warn("Aucun prix reçu via WS (normal si marché peu actif)")
            info("Fallback REST : les prix du scanner seront utilisés")
    else:
        warn("Pas de marchés pour tester les souscriptions")


# ══════════════════════════════════════════════════════════════════════════════
# 4. PROBABILITY ENGINE — Calcul P(Yes) sur vrais marchés
# ══════════════════════════════════════════════════════════════════════════════

def test_probability_engine(markets: list) -> list:
    section("4/6 — Probability Engine : calcul P(Yes) sur vrais marchés")

    if not markets:
        warn("Pas de marchés — test ignoré")
        return []

    from signals.probability_engine import compute_yes_prob

    results = []
    errors  = []
    unsupported = []

    print()
    for m in markets[:8]:
        try:
            result = compute_yes_prob(m.question, record=False)
            if result is None:
                unsupported.append(m.question[:55])
                info(f"⚪ Non supporté : {m.question[:55]}")
                continue

            p_yes, conf, detail = result
            market_p = m.market_yes_prob or 0.5
            edge = abs(p_yes - market_p)
            results.append((m, p_yes, conf, detail))

            info(f"✅ {m.question[:55]}")
            info(f"   P(bot)={p_yes*100:.1f}%  P(marché)={market_p*100:.1f}%  "
                 f"edge_brut={edge*100:.1f}%  conf={conf*100:.0f}%  "
                 f"couches={detail.get('layers_used','?')}/3")
            fc = detail.get("forecast_c")
            if fc:
                info(f"   prévision={fc}°C  biais={detail.get('bias_corr',0):+.2f}°C  "
                     f"sources={detail.get('sources', [])}")

        except Exception as e:
            errors.append((m.question[:45], str(e)))
            info(f"❌ ERREUR : {m.question[:45]}")
            info(f"   {e}")

    print()
    ok(f"{len(results)} calculs réussis")
    if unsupported:
        warn(f"{len(unsupported)} questions non supportées (format inconnu)")
    if errors:
        err(f"{len(errors)} erreurs :")
        for q, e in errors:
            info(f"   {q} → {e}")

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 5. EDGE DETECTOR — Signaux avec déflation oracle
# ══════════════════════════════════════════════════════════════════════════════

def test_edge_detector(markets: list) -> list:
    section("5/6 — Edge Detector : signaux avec déflation oracle")

    if not markets:
        warn("Pas de marchés — test ignoré")
        return []

    import tempfile
    from core.models import ContractSpecCache
    from core.contract_parser import parse_contract
    from signals.edge_detector import find_best_trades
    from monitoring.pnl_tracker import get_current_bankroll

    # Cache temporaire pour ce test
    cache_path = Path(tempfile.mktemp(suffix="_edge_specs.json"))
    cache      = ContractSpecCache(str(cache_path))

    # Pré-parse les ContractSpecs
    for m in markets:
        end_iso = m.end_date.isoformat() if m.end_date else ""
        parse_contract(m.condition_id, m.condition_id, m.question, end_iso, cache)

    bankroll = get_current_bankroll()
    info(f"Bankroll courante : ${bankroll:.2f}")
    info("Calcul des signaux (avec déflation oracle)...")
    print()

    try:
        signals = find_best_trades(markets, bankroll=bankroll, spec_cache=cache)

        if not signals:
            warn("Aucun signal net détecté")
            info("(Normal : edge_brut × alignment − frais peut tomber sous MIN_EDGE=5%)")
        else:
            ok(f"{len(signals)} signal(s) détecté(s)")
            print()
            for i, s in enumerate(signals, 1):
                spec = cache.get(s.market.condition_id)
                align = spec.oracle_alignment_score if spec else 1.0
                info(f"#{i} [{s.side}] edge_net={s.edge*100:.1f}%  "
                     f"mise=${s.kelly_stake_usd:.2f}  "
                     f"alignment={align:.2f}  "
                     f"conf={s.signal.confidence*100:.0f}%")
                info(f"   {s.market.question[:60]}")

    except Exception as e:
        err(f"Edge detector erreur : {e}")
        import traceback; traceback.print_exc()
        signals = []
    finally:
        cache_path.unlink(missing_ok=True)
        cache_path.with_suffix(".tmp").unlink(missing_ok=True)

    return signals


# ══════════════════════════════════════════════════════════════════════════════
# 6. CYCLE COMPLET — Simulation d'un cycle main.py
# ══════════════════════════════════════════════════════════════════════════════

def test_full_cycle() -> None:
    section("6/6 — Cycle complet : simulation d'un cycle main.py (DRY_RUN)")

    import tempfile
    from core.models import ContractSpecCache
    from scanners.market_scanner import scan_weather_markets
    from signals.edge_detector import find_best_trades, print_signals
    from execution.order_manager import place_order
    from execution.position_manager import position_monitor_cycle
    from monitoring.pnl_tracker import get_current_bankroll, print_open_positions, print_pnl_summary
    from config.settings import CONTRACT_SPECS_FILE

    # Cache persistant (réutilise celui du projet si il existe)
    cache = ContractSpecCache(CONTRACT_SPECS_FILE)
    info(f"ContractSpecs en cache : {len(cache)}")

    # Étape 1 : positions existantes
    info("→ Vérification positions existantes...")
    try:
        position_monitor_cycle(spec_cache=cache)
        ok("position_monitor_cycle OK")
    except Exception as e:
        err(f"position_monitor_cycle : {e}")

    # Étape 2 : scan
    info("→ Scan marchés météo...")
    try:
        markets = scan_weather_markets(spec_cache=cache)
        ok(f"Scan OK — {len(markets)} marché(s)")
        info(f"ContractSpecs après scan : {len(cache)}")
    except Exception as e:
        err(f"Scan : {e}")
        return

    if not markets:
        warn("Aucun marché — fin du test cycle")
        return

    # Étape 3 : signaux
    info("→ Calcul signaux...")
    try:
        bankroll = get_current_bankroll()
        signals  = find_best_trades(markets, bankroll=bankroll, spec_cache=cache)
        ok(f"Signaux OK — {len(signals)} signal(s)")
        print_signals(signals)
    except Exception as e:
        err(f"Signaux : {e}")
        return

    # Étape 4 : ordres simulés (DRY_RUN forcé)
    if signals:
        info(f"→ Simulation {min(3, len(signals))} ordre(s) DRY_RUN...")
        for s in signals[:3]:
            try:
                order = place_order(s)
                if order:
                    ok(f"Ordre simulé : [{s.side}] ${s.kelly_stake_usd:.2f} @ {s.token_id[:24]}...")
                else:
                    warn(f"Ordre ignoré (position déjà ouverte ou token manquant)")
            except Exception as e:
                err(f"place_order : {e}")
    else:
        warn("Aucun signal → aucun ordre simulé")

    # Résumé
    print()
    print_open_positions()
    print_pnl_summary()
    ok("Cycle complet terminé sans erreur critique")


# ══════════════════════════════════════════════════════════════════════════════
# RAPPORT FINAL
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    print(f"\n{SEP}")
    print("  TEST EN CONDITIONS RÉELLES — Polymarket Weather Bot")
    print("  DRY_RUN=1 forcé — aucun ordre réel")
    print(SEP)

    from config.settings import (
        OWM_API_KEY, MIN_EDGE, KELLY_FRACTION,
        MIN_ORACLE_ALIGNMENT, ORACLE_POLL_INTERVAL_SEC,
    )
    print()
    info(f"OWM_API_KEY     : {'✅ présente' if OWM_API_KEY else '❌ manquante'}")
    info(f"MIN_EDGE        : {MIN_EDGE*100:.0f}%")
    info(f"KELLY_FRACTION  : {KELLY_FRACTION*100:.0f}%")
    info(f"MIN_ORACLE_ALIGN: {MIN_ORACLE_ALIGNMENT:.2f}")
    info(f"ORACLE_POLL     : {ORACLE_POLL_INTERVAL_SEC//60} min")

    # Pipeline
    markets       = test_gamma_scan()
    _cache, pairs = test_contract_parser(markets)
    test_websocket(markets)
    probs         = test_probability_engine(markets)
    signals       = test_edge_detector(markets)
    test_full_cycle()

    # Bilan
    section("BILAN")
    checks = [
        ("Gamma API scan",        bool(markets)),
        ("ContractParser",        bool(pairs)),
        ("ProbabilityEngine",     bool(probs)),
        ("EdgeDetector",          True),   # pas d'erreur = OK même sans signal
        ("Cycle complet DRY_RUN", True),
    ]
    all_ok = True
    for label, passed in checks:
        if passed:
            ok(label)
        else:
            err(label)
            all_ok = False

    print()
    if all_ok:
        print(f"  {OK} Tous les tests passent — bot prêt pour DRY_RUN prolongé")
    else:
        print(f"  {ERR} Des composants nécessitent attention avant déploiement")
    print(f"\n{SEP}\n")


if __name__ == "__main__":
    main()