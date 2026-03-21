"""
Compare P(bot) vs P(marché) et identifie les edges tradables.
Compatible avec probability_engine v2 (retourne tuple de 3).
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from typing import List, Optional
from core.models import WeatherMarket, WeatherSignal, TradeSignal
from signals.probability_engine import compute_yes_prob
from config.settings import MIN_EDGE, MAX_BET_USD, KELLY_FRACTION

TAKER_FEE   = 0.02
SPREAD_COST = 0.025
TOTAL_COST  = TAKER_FEE + SPREAD_COST

def kelly_stake(p_win: float, odds: float, bankroll: float) -> float:
    q_win = 1 - p_win
    k     = max(0.0, (p_win * odds - q_win) / odds)
    k    *= KELLY_FRACTION
    return round(min(k * bankroll, MAX_BET_USD), 2)

def analyze_market(market: WeatherMarket, bankroll: float) -> Optional[TradeSignal]:
    result = compute_yes_prob(market.question, record=True)
    if result is None:
        return None
    our_prob, confidence, detail = result
    signal = WeatherSignal(
        market_condition_id=market.condition_id,
        our_yes_prob=our_prob,
        confidence=confidence,
        sources=detail.get("sources", []),
    )
    market_yes_prob = market.market_yes_prob or 0.5
    edge_yes = our_prob - market_yes_prob
    edge_no  = (1 - our_prob) - (1 - market_yes_prob)
    side      = "YES" if edge_yes >= edge_no else "NO"
    best_edge = abs(edge_yes if side == "YES" else edge_no)
    net_edge  = best_edge - TOTAL_COST
    if side == "YES":
        return None  # YES désactivé — winrate historique 0%
    if net_edge < MIN_EDGE:
        return None
    token = market.yes_token if side == "YES" else market.no_token
    if token is None:
        return None
    entry_price = token.price
    if entry_price <= 0 or entry_price >= 1:
        return None
    odds  = (1 / entry_price) - 1
    p_win = our_prob if side == "YES" else (1 - our_prob)
    stake = kelly_stake(p_win, odds, bankroll)
    if stake < 1.0:
        return None
    return TradeSignal(
        market=market, signal=signal, side=side,
        edge=net_edge, kelly_stake_usd=stake, token_id=token.token_id,
    )

MAX_OPEN_POSITIONS    = 6     # Maximum positions simultanées
MAX_EXPOSURE_PCT      = 0.30  # Max 40% de la bankroll exposée simultanément
MAX_PER_CITY          = 1     # Max 2 positions par ville
MAX_PER_EXPIRY        = 3     # Max 3 positions par date d'expiry
STOP_LOSS_PCT         = 0.20  # Arrêt si bankroll chute de 25%
INITIAL_BANKROLL      = 150.0

def find_best_trades(markets: List[WeatherMarket], bankroll: float = 25.0) -> List[TradeSignal]:
    from execution.position_manager import get_open_positions

    # Stop-loss global
    drawdown = (INITIAL_BANKROLL - bankroll) / INITIAL_BANKROLL
    if drawdown > STOP_LOSS_PCT:
        print(f"  ⛔ STOP-LOSS déclenché — bankroll=${bankroll:.2f} < ${INITIAL_BANKROLL*(1-STOP_LOSS_PCT):.2f}")
        return []

    # Positions ouvertes existantes
    open_positions = get_open_positions()
    open_count     = len(open_positions)
    open_exposure  = sum(p.get("size_usd", 0) for p in open_positions)
    cities_open    = {}
    expiry_open    = {}
    for p in open_positions:
        import re
        q_p    = p.get("question", "")
        m_p    = re.search(r'temperature in ([A-Za-z\s]+?) (be|on)', q_p, re.IGNORECASE)
        city   = m_p.group(1).strip() if m_p else ""
        expiry = (p.get("end_date") or "")[:10]
        cities_open[city]   = cities_open.get(city, 0) + 1
        expiry_open[expiry] = expiry_open.get(expiry, 0) + 1
        cities_open[city]   = cities_open.get(city, 0) + 1
        expiry_open[expiry] = expiry_open.get(expiry, 0) + 1

    # Génère tous les signaux
    all_signals = [sig for m in markets if (sig := analyze_market(m, bankroll))]
    all_signals.sort(key=lambda s: s.edge, reverse=True)

    filtered   = []
    new_exposure = 0.0

    for s in all_signals:
        import re
        q    = s.market.question or ""
        m_city = re.search(r'temperature in ([A-Za-z\s]+?) (be|on)', q, re.IGNORECASE)
        city = m_city.group(1).strip() if m_city else q[:20]
        end = s.market.end_date
        expiry = end.strftime("%Y-%m-%d") if hasattr(end, 'strftime') else str(end)[:10]

        # Limite positions totales
        if open_count + len(filtered) >= MAX_OPEN_POSITIONS:
            print(f"  ⚠️  Max positions atteint ({MAX_OPEN_POSITIONS})")
            break

        # Limite exposition totale
        if (open_exposure + new_exposure + s.kelly_stake_usd) > bankroll * MAX_EXPOSURE_PCT:
            print(f"  ⚠️  Exposition max atteinte ({MAX_EXPOSURE_PCT*100:.0f}% bankroll)")
            continue

        # Limite par ville
        if cities_open.get(city, 0) >= MAX_PER_CITY:
            print(f"  ⚠️  Max positions pour {city} atteint ({MAX_PER_CITY})")
            continue

        # Limite par date d'expiry
        if expiry_open.get(expiry, 0) >= MAX_PER_EXPIRY:
            print(f"  ⚠️  Max positions pour expiry {expiry} atteint ({MAX_PER_EXPIRY})")
            continue

        filtered.append(s)
        new_exposure += s.kelly_stake_usd
        cities_open[city]   = cities_open.get(city, 0) + 1
        expiry_open[expiry] = expiry_open.get(expiry, 0) + 1

    return filtered

def print_signals(signals: List[TradeSignal]) -> None:
    if not signals:
        print("  Aucun edge net détecté (après frais)."); return
    print(f"\n{'═'*72}")
    print(f"{'OPPORTUNITÉS (edge net après frais)':^72}")
    print(f"{'═'*72}")
    for i, s in enumerate(signals, 1):
        print(f"\n#{i:02d} [{s.side}] Edge net=+{s.edge*100:.1f}%  Mise=${s.kelly_stake_usd:.2f}  Conf={s.signal.confidence*100:.0f}%")
        print(f"     {s.market.question}")
        print(f"     P(bot)={s.signal.our_yes_prob*100:.1f}%  P(marché)={s.market.market_yes_prob*100:.1f}%")
        print(f"     token_id={s.token_id[:32]}...")
    print(f"\n{'═'*72}\nTotal opportunités nettes : {len(signals)}")

if __name__ == "__main__":
    from scanners.market_scanner import scan_weather_markets
    markets = scan_weather_markets()
    print(f"\nAnalyse de {len(markets)} marchés...")
    signals = find_best_trades(markets, bankroll=25.0)
    print_signals(signals)