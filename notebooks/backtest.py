"""
Backtester fiable — utilise le cache généré par history_scraper.py
(vrais prix CLOB + météo archive Open-Meteo).

Usage :
    python notebooks/backtest.py
    python notebooks/backtest.py --min-edge 0.03
    python notebooks/backtest.py --kelly 0.5
"""
from __future__ import annotations
import json, math, sys, os, urllib.request, urllib.parse, time, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple

DATA_DIR      = os.path.join(os.path.dirname(__file__), "..", "data", "raw")
MARKETS_FILE  = os.path.join(DATA_DIR, "historical_markets.json")
PRICES_FILE   = os.path.join(DATA_DIR, "historical_prices.json")
ARCHIVE_URL   = "https://archive-api.open-meteo.com/v1/archive"
GEOCODE_URL   = "https://geocoding-api.open-meteo.com/v1/search"
HISTORY_YEARS = 15

_geo_cache:     Dict[str, Optional[Tuple[float, float]]] = {}
_climate_cache: Dict[str, List[float]] = {}

def _get(url: str, delay: float = 0.3) -> dict:
    time.sleep(delay)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def _load(path: str) -> list:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return []

def _geocode(city: str) -> Optional[Tuple[float, float]]:
    if city in _geo_cache: return _geo_cache[city]
    try:
        data   = _get(f"{GEOCODE_URL}?name={urllib.parse.quote(city)}&count=1&language=en")
        res    = data.get("results")
        coords = (res[0]["latitude"], res[0]["longitude"]) if res else None
    except Exception:
        coords = None
    _geo_cache[city] = coords
    return coords

def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    z = (x - mu) / (sigma * math.sqrt(2))
    return 0.5 * (1 + math.erf(z))

def get_forecast_proxy(city: str, date_str: str) -> Optional[float]:
    coords = _geocode(city)
    if not coords: return None
    lat, lon = coords
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start  = (target - timedelta(days=3)).strftime("%Y-%m-%d")
        end    = (target - timedelta(days=1)).strftime("%Y-%m-%d")
        data   = _get(f"{ARCHIVE_URL}?latitude={lat}&longitude={lon}"
                      f"&start_date={start}&end_date={end}"
                      f"&daily=temperature_2m_max&timezone=auto")
        temps  = [t for t in data.get("daily", {}).get("temperature_2m_max", [])
                  if t is not None]
        return round(sum(temps) / len(temps), 2) if temps else None
    except Exception:
        return None

def get_climate_temps(city: str, month: int) -> List[float]:
    key = f"{city}_{month}"
    if key in _climate_cache: return _climate_cache[key]
    coords = _geocode(city)
    if not coords: return []
    lat, lon = coords
    end_yr   = datetime.now(timezone.utc).year - 1
    all_temps: List[float] = []
    for year in range(end_yr - HISTORY_YEARS + 1, end_yr + 1):
        last_day = 31 if month == 12 else (datetime(year, month + 1, 1) - timedelta(days=1)).day
        try:
            data  = _get(f"{ARCHIVE_URL}?latitude={lat}&longitude={lon}"
                         f"&start_date={year}-{month:02d}-01"
                         f"&end_date={year}-{month:02d}-{last_day:02d}"
                         f"&daily=temperature_2m_max&timezone=auto")
            temps = data.get("daily", {}).get("temperature_2m_max", [])
            all_temps.extend(t for t in temps if t is not None)
        except Exception:
            continue
    _climate_cache[key] = all_temps
    return all_temps
def _compute_prob_with_forecast(city: str, target_c: float, modifier: str,
                                 date_str: str, forecast: float) -> Optional[Tuple[float, float]]:
    try:
        month = datetime.strptime(date_str, "%Y-%m-%d").month
    except Exception:
        return None
    sigma = 3.5 if modifier == "exact" else 2.0
    hist  = get_climate_temps(city, month)
    if modifier == "above":
        p_fc   = 1 - _normal_cdf(target_c, forecast, sigma)
        p_clim = sum(1 for t in hist if t >= target_c) / len(hist) if hist else None
    elif modifier == "below":
        p_fc   = _normal_cdf(target_c, forecast, sigma)
        p_clim = sum(1 for t in hist if t <= target_c) / len(hist) if hist else None
    else:
        p_fc   = (_normal_cdf(target_c + 0.5, forecast, sigma)
                - _normal_cdf(target_c - 0.5, forecast, sigma))
        p_clim = sum(1 for t in hist if abs(t - target_c) <= 0.5) / len(hist) if hist else None
    if p_clim is not None and len(hist) >= 10:
        return round(max(0.02, min(0.98, 0.65 * p_fc + 0.35 * p_clim)), 4), 0.80
    return round(max(0.02, min(0.98, p_fc)), 4), 0.60
def compute_prob(city: str, target_c: float, modifier: str,
                 date_str: str) -> Optional[Tuple[float, float]]:
    try:
        month = datetime.strptime(date_str, "%Y-%m-%d").month
    except Exception:
        return None
    forecast = get_forecast_proxy(city, date_str)
    if forecast is None:
        return None
    sigma = 3.5 if modifier == "exact" else 2.0
    hist  = get_climate_temps(city, month)
    if modifier == "above":
        p_fc   = 1 - _normal_cdf(target_c, forecast, sigma)
        p_clim = sum(1 for t in hist if t >= target_c) / len(hist) if hist else None
    elif modifier == "below":
        p_fc   = _normal_cdf(target_c, forecast, sigma)
        p_clim = sum(1 for t in hist if t <= target_c) / len(hist) if hist else None
    else:
        p_fc   = (_normal_cdf(target_c + 0.5, forecast, sigma)
                - _normal_cdf(target_c - 0.5, forecast, sigma))
        p_clim = sum(1 for t in hist if abs(t - target_c) <= 0.5) / len(hist) if hist else None
    if p_clim is not None and len(hist) >= 10:
        return round(max(0.02, min(0.98, 0.65 * p_fc + 0.35 * p_clim)), 4), 0.80
    return round(max(0.02, min(0.98, p_fc)), 4), 0.60

def simulate_trade(market: dict, prices_map: Dict[str, dict],
                   bankroll: float, min_edge: float,
                   kelly_frac: float, max_bet: float) -> Optional[dict]:
    cid      = market["condition_id"]
    yes_p    = prices_map.get(cid, {}).get("yes_price")
    if yes_p is None: return None
    no_p     = round(1.0 - yes_p, 4)
    if market.get("actual_temp_max") is not None:
        result = _compute_prob_with_forecast(
            market["city"], market["target_c"],
            market["modifier"], market["end_date"],
            market["actual_temp_max"]
        )
    else:
        result = compute_prob(market["city"], market["target_c"],
                              market["modifier"], market["end_date"])
    if result is None: return None
    our_prob, conf = result
    ey   = our_prob - yes_p
    en   = (1 - our_prob) - no_p
    side = "YES" if ey >= en else "NO"
    net  = abs(ey if side == "YES" else en) - 0.045
    if net < min_edge: return None
    # YES uniquement si marché sous-estime fortement ET edge très élevé
    if side == "YES" and net < 0.15:
        return None
    ep   = yes_p if side == "YES" else no_p
    if not (0 < ep < 1): return None
    p_win = our_prob if side == "YES" else (1 - our_prob)
    odds  = (1 / ep) - 1
    k     = max(0.0, (p_win * odds - (1 - p_win)) / odds) * kelly_frac
    stake = round(min(k * bankroll, max_bet), 2)
    if stake < 5.0: return None
    won = (market["yes_won"] and side == "YES") or (not market["yes_won"] and side == "NO")
    pnl = stake * (1.0 / ep - 1.0) - stake * 0.02 if won else -stake
    return {
        "question": market["question"][:65], "city": market["city"],
        "date": market["end_date"], "side": side,
        "our_prob": round(our_prob * 100, 1), "market_prob": round(ep * 100, 1),
        "edge_net": round(net * 100, 1), "stake": stake,
        "won": won, "pnl": round(pnl, 2), "confidence": round(conf * 100),
    }

def print_report(trades: List[dict], capital: float, params: dict) -> None:
    if not trades:
        print("\n  Aucun trade simulé.")
        print("  → Essaie : python notebooks/backtest.py --min-edge 0.03")
        print("  → Enrichis : python notebooks/history_scraper.py")
        return
    wins  = [t for t in trades if t["won"]]
    total = len(trades)
    pnl   = sum(t["pnl"] for t in trades)
    print(f"\n{'═'*72}")
    print(f"  BACKTEST — Capital ${capital:.0f} | "
          f"min_edge={params['min_edge']*100:.0f}% | kelly={params['kelly']}")
    print(f"{'═'*72}")
    print(f"  Trades  : {total}  (✅ {len(wins)}  ❌ {total-len(wins)})")
    print(f"  Winrate : {len(wins)/total*100:.1f}%")
    print(f"  PnL net : ${pnl:+.2f}  |  ROI : {pnl/capital*100:+.1f}%")
    if wins:
        print(f"  Gain moy: ${sum(t['pnl'] for t in wins)/len(wins):+.2f}  "
              f"Perte moy: ${sum(t['pnl'] for t in trades if not t['won'])/max(total-len(wins),1):+.2f}")
    print(f"\n  {'Date':<12} {'Ville':<16} {'Side':>4} "
          f"{'Bot':>6} {'Mkt':>6} {'Edge':>6} {'Mise':>6} {'PnL':>8}")
    print(f"  {'─'*66}")
    for t in sorted(trades, key=lambda x: x["date"]):
        print(f"  {t['date']:<12} {t['city']:<16} {t['side']:>4} "
              f"{t['our_prob']:>5.1f}% {t['market_prob']:>5.1f}% "
              f"{t['edge_net']:>5.1f}% ${t['stake']:>5.2f} "
              f"${t['pnl']:>+7.2f}  {'✅' if t['won'] else '❌'}")
    print(f"\n{'═'*72}")
    if total >= 10:
        print(f"  {'✅ PROFITABLE' if pnl > 0 else '❌ NON PROFITABLE'} "
              f"— ~${pnl/max(total/15,1):+.2f}/semaine estimé")
    else:
        print(f"  ⚠️  {total} trades seulement — besoin ≥10 pour conclure.")
        print(f"      Les marchés température Polymarket sont un format récent (~fin 2025).")
        print(f"      Validation principale = DRY-RUN : python main.py")
    print(f"{'═'*72}\n")

def run_backtest(capital: float = 150.0, min_edge: float = 0.05,
                 kelly: float = 0.25, max_bet: float = 20.0) -> None:
    master_file = os.path.join(DATA_DIR, "master_weather.json")
    if not os.path.exists(master_file):
        print("❌ master_weather.json absent. Lance :")
        print("   python notebooks/build_master.py"); return

    master = _load(master_file)
    # Construit prices_map depuis le master directement
    prices_map = {
        m["condition_id"]: {"yes_price": m["yes_price_clob"]}
        for m in master if m.get("yes_price_clob") is not None
    }
    resolved = [
        m for m in master
        if m.get("resolved") is True
        and m.get("yes_won") is not None
        and m.get("yes_price_clob") is not None
        and m.get("city")
        and m.get("target_c") is not None
        and m.get("modifier")
        and m.get("end_date")
    ]

    print(f"Master : {len(master):,} marchés | {len(resolved):,} résolus avec prix réels\n")
    if not resolved:
        print("❌ Aucun marché résolu avec prix."); return

    print(f"Simulation ${capital:.0f} "
          f"(min_edge={min_edge*100:.0f}% kelly={kelly} max_bet=${max_bet})...\n")

    trades   = []
    bankroll = capital
    skips    = {"prix": 0, "météo": 0, "edge": 0, "mise": 0}

    for i, market in enumerate(resolved):
        print(f"  [{i+1:>3}/{len(resolved)}] {market['city']:<18} {market['end_date']} "
              f"target={market['target_c']:>5.1f}°C ... ", end="", flush=True)
        pdata = prices_map.get(market["condition_id"], {})
        if not pdata.get("yes_price"):
            skips["prix"] += 1; print("skip(prix)"); continue
        forecast = market.get("forecast_tmax_j2") or market.get("tmax_j1") or market.get("actual_temp_max")
        if forecast is not None:
            result = _compute_prob_with_forecast(
                market["city"], market["target_c"],
                market["modifier"], market["end_date"],
                forecast
            )
        else:
            result = compute_prob(market["city"], market["target_c"],
                                  market["modifier"], market["end_date"])
        if result is None:
            skips["météo"] += 1; print("skip(météo archive)"); continue
        our_prob, _ = result
        yp   = pdata["yes_price"]
        ey   = our_prob - yp
        en   = (1 - our_prob) - (1 - yp)
        side = "YES" if ey >= en else "NO"
        if side == "YES":
            skips["edge"] += 1; print("skip(YES désactivé)"); continue
        net  = abs(en) - 0.045
        if net < min_edge:
            skips["edge"] += 1
            print(f"skip(edge={net*100:.1f}% bot={our_prob*100:.0f}% mkt={yp:.2f})")
            continue
        trade = simulate_trade(market, prices_map, bankroll, min_edge, kelly, max_bet)
        if trade:
            trades.append(trade)
            bankroll += trade["pnl"]
            print(f"{'✅' if trade['won'] else '❌'} ${trade['pnl']:+.2f} → ${bankroll:.2f}")
        else:
            skips["mise"] += 1; print("skip(mise<$5)")

    if any(v for v in skips.values()):
        print(f"\n  Skips : {skips}")
    print_report(trades, capital, {"min_edge": min_edge, "kelly": kelly})

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital",  type=float, default=150.0)
    parser.add_argument("--min-edge", type=float, default=0.05)
    parser.add_argument("--kelly",    type=float, default=0.25)
    parser.add_argument("--max-bet",  type=float, default=20.0)
    args = parser.parse_args()
    run_backtest(capital=args.capital, min_edge=args.min_edge,
                 kelly=args.kelly, max_bet=args.max_bet)