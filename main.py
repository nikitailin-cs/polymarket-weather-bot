"""
Point d'entrée principal — bot Polymarket Weather.
- Persistance JSON (survit aux crashes)
- Suivi des positions ouvertes
- Tracker PnL en temps réel
"""
import sys, time
from config.settings import DRY_RUN, WEATHER_REFRESH_SEC
from monitoring.logger import log
from monitoring.telegram_alert import alert_start, alert_stop, alert_error
from monitoring.pnl_tracker import print_open_positions, print_pnl_summary
from scanners.market_scanner import scan_weather_markets
from signals.edge_detector import find_best_trades, print_signals
from execution.order_manager import place_order
from execution.position_manager import position_monitor_cycle


def run() -> None:
    mode = "DRY-RUN (simulation)" if DRY_RUN else "⚠️  LIVE — ordres réels"
    log.info("=" * 52)
    log.info(f"  Bot démarré — Mode : {mode}")
    log.info("=" * 52)
    alert_start()

    print_open_positions()
    print_pnl_summary()

    cycle = 0
    try:
        while True:
            cycle += 1
            log.info(f"── Cycle #{cycle} ──────────────────────────────────")

            # 1. Vérifier les positions existantes
            position_monitor_cycle()

            # 2. Scanner les marchés météo actifs
            markets = scan_weather_markets()
            if not markets:
                log.warning("Aucun marché météo actif — pause 5 min")
                time.sleep(300)
                continue

            # 3. Trouver les edges
            from monitoring.pnl_tracker import get_current_bankroll
            signals = find_best_trades(markets, bankroll=get_current_bankroll())
            print_signals(signals)

            # 4. Passer les ordres (top 3 par edge)
            for signal in signals[:3]:
                place_order(signal)

            # 5. Résumé PnL toutes les 10 cycles
            if cycle % 10 == 0:
                print_open_positions()
                print_pnl_summary()

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


if __name__ == "__main__":
    run()