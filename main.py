"""
Point d'entrée principal — bot Polymarket Weather.
- Persistance JSON (survit aux crashes)
- Suivi des positions ouvertes
- Tracker PnL en temps réel
- v2 : ContractSpecCache injecté dans scanner, edge_detector, position_manager
- v2.1 : update_actuals() appelé toutes les 10 cycles pour calibration modèle
"""
import os
import sys, time
from config.settings import DRY_RUN, WEATHER_REFRESH_SEC, CONTRACT_SPECS_FILE
from monitoring.logger import log

if os.getenv("MAIN_DEPLOY_BANNER", "").strip().lower() in ("1", "true", "yes", "on"):
    log.info(
        "MAIN_DEPLOY_BANNER=1 — message historique de déploiement (anciennement print stdout)"
    )
from monitoring.telegram_alert import alert_start, alert_stop, alert_error
from monitoring.error_guard import run_guarded
from monitoring.pnl_tracker import print_open_positions, print_pnl_summary
from monitoring.onchain_logger import print_onchain_summary
from signals.model_registry import print_model_info
from signals.feature_store import print_store_summary
from scanners.market_scanner import scan_weather_markets
from scanners.ws_price_feed import start_ws_feed, subscribe_tokens, get_price
from signals.edge_detector import find_best_trades, print_signals
from execution.order_manager import place_order
from execution.position_manager import position_monitor_cycle
from core.models import ContractSpecCache

# Calibration : mise à jour des températures réelles observées
# Toutes les N cycles → réduit les appels à l'API archive
CALIBRATION_EVERY_N = 10


def run() -> None:
    mode = "DRY-RUN (simulation)" if DRY_RUN else "⚠️  LIVE — ordres réels"
    log.info("=" * 52)
    log.info(f"  Bot démarré — Mode : {mode}")
    log.info("=" * 52)

    alert_start()
    start_ws_feed()
    time.sleep(2)

    spec_cache = ContractSpecCache(CONTRACT_SPECS_FILE)
    log.info(f"  ContractSpecs en cache : {len(spec_cache)}")

    print_open_positions()
    print_pnl_summary()
    print_model_info()
    print_store_summary()

    cycle = 0
    try:
        while True:
            cycle += 1
            log.info(f"── Cycle #{cycle} ──────────────────────────────────")

            # 1. Vérifier les positions existantes
            position_monitor_cycle(spec_cache=spec_cache)

            # 2. Scanner les marchés météo actifs
            markets = scan_weather_markets(spec_cache=spec_cache)
            if not markets:
                log.warning("Aucun marché météo actif — pause 5 min")
                time.sleep(300)
                continue

            # Abonne le WS aux tokens des marchés actifs
            all_token_ids = [t.token_id for m in markets for t in m.tokens if t.token_id]
            subscribe_tokens(all_token_ids)

            # Met à jour les prix depuis le cache WS si disponible
            for market in markets:
                for token in market.tokens:
                    ws_price = get_price(token.token_id)
                    if ws_price is not None:
                        token.price = ws_price

            # 3. Trouver les edges
            from monitoring.pnl_tracker import get_current_bankroll
            signals = find_best_trades(
                markets,
                bankroll   = get_current_bankroll(),
                spec_cache = spec_cache,
            )
            print_signals(signals)

            # 4. Passer les ordres (top 3 par edge)
            for signal in signals[:3]:
                place_order(signal)

            # 5. Toutes les N cycles :
            #    - Résumé PnL
            #    - Mise à jour calibration modèle (températures réelles observées)
            if cycle % CALIBRATION_EVERY_N == 0:
                print_open_positions()
                print_pnl_summary()
                print_onchain_summary()
                _run_calibration()

            log.info(f"Cycle #{cycle} terminé — prochain dans {WEATHER_REFRESH_SEC}s")
            time.sleep(WEATHER_REFRESH_SEC)

    except KeyboardInterrupt:
        log.info("Arrêt manuel (Ctrl+C)")
        print_open_positions()
        print_pnl_summary()
        alert_stop("Arrêt manuel")

    except Exception as e:
        log.exception(f"Erreur critique : {e}")
        alert_error(str(e))
        sys.exit(1)


def _run_calibration() -> None:
    """
    Met à jour les températures réelles observées pour calibrer les biais de modèle.
    Appelé périodiquement — lent (appels réseau archive) mais exécuté hors boucle chaude.
    """
    try:
        from signals.sources.model_calibrator import update_actuals
        n = update_actuals()
        if n > 0:
            log.info(f"  🔧 Calibration : {n} observation(s) mise(s) à jour")
    except Exception as e:
        log.warning(f"  Calibration échouée (non critique) : {e}")


if __name__ == "__main__":
    raise SystemExit(run_guarded("main.bot", lambda: (run() or 0)))