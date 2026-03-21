"""
Scraper historique complet — récupère TOUT l'historique des marchés
température Polymarket + vrais prix CLOB pre-résolution.

Usage :
    python notebooks/history_scraper.py          # scrape + prix
    python notebooks/history_scraper.py --prices # re-fetch prix seulement
"""
from __future__ import annotations
import json, re, sys, os, urllib.request, urllib.parse, time, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple

OUT_DIR      = os.path.join(os.path.dirname(__file__), "..", "data", "raw")
MARKETS_FILE = os.path.join(OUT_DIR, "historical_markets.json")
PRICES_FILE  = os.path.join(OUT_DIR, "historical_prices.json")
GAMMA_URL    = "https://gamma-api.polymarket.com/markets"
CLOB_URL     = "https://clob.polymarket.com"

TEMP_PATTERN = re.compile(
    r"(?:highest|lowest)?\s*temperature in ([A-Za-z\s\-]+?) be "
    r"(\d+(?:\.\d+)?)\s*°([CF])"
    r"(\s+or\s+(?:below|above|higher|lower))?",
    re.IGNORECASE
)

def _get(url: str, delay: float = 0.25) -> dict:
    time.sleep(delay)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def _save(path: str, data: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def _load(path: str) -> list:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return []

def _parse(m: dict) -> Optional[dict]:
    q = m.get("question") or ""
    if "temperature" not in q.lower():
        return None
    match = TEMP_PATTERN.search(q)
    if not match:
        return None

    prices_raw = m.get("outcomePrices") or []
    try:
        if isinstance(prices_raw, str): prices_raw = json.loads(prices_raw)
        prices = [float(p) for p in prices_raw]
    except Exception:
        prices = []
    resolved = bool(prices) and max(prices) >= 0.95

    outcomes_raw = m.get("outcomes") or '["Yes","No"]'
    try:
        if isinstance(outcomes_raw, str): outcomes_raw = json.loads(outcomes_raw)
        outcomes = outcomes_raw
    except Exception:
        outcomes = ["Yes", "No"]

    yes_won = None
    if resolved and prices:
        winner_idx = prices.index(max(prices))
        winner     = outcomes[winner_idx] if winner_idx < len(outcomes) else "Yes"
        yes_won    = winner.lower() == "yes"

    city = match.group(1).strip()
    temp = float(match.group(2))
    unit = match.group(3).upper()
    mod  = (match.group(4) or "").strip().lower()
    if unit == "F": temp = (temp - 32) * 5 / 9
    if "below" in mod or "lower" in mod: mod = "below"
    elif "above" in mod or "higher" in mod: mod = "above"
    else: mod = "exact"

    token_ids_raw = m.get("clobTokenIds") or "[]"
    try:
        if isinstance(token_ids_raw, str): token_ids_raw = json.loads(token_ids_raw)
        token_ids = token_ids_raw
    except Exception:
        token_ids = []

    yes_token_id = None
    if token_ids and outcomes:
        idx = 0 if outcomes[0].lower() == "yes" else 1
        yes_token_id = token_ids[idx] if idx < len(token_ids) else token_ids[0]

    return {
        "condition_id":  m.get("conditionId") or "",
        "question":      q,
        "city":          city,
        "target_c":      round(temp, 2),
        "modifier":      mod,
        "end_date":      (m.get("endDate") or "")[:10],
        "resolved":      resolved,
        "yes_won":       yes_won,
        "yes_token_id":  yes_token_id,
        "volume":        float(m.get("volume") or 0),
        "active":        m.get("active", False),
        "accepting":     m.get("acceptingOrders", False),
    }

def _scrape_strategy(params: str, label: str, found: dict, max_pages: int = 100) -> int:
    new = 0
    for page in range(max_pages):
        offset = page * 100
        try:
            data = _get(f"{GAMMA_URL}?{params}&limit=100&offset={offset}", delay=0.2)
            if not data:
                break
            for m in data:
                parsed = _parse(m)
                if parsed and parsed["condition_id"] and parsed["condition_id"] not in found:
                    found[parsed["condition_id"]] = parsed
                    new += 1
            if len(data) < 100:
                break
        except Exception as e:
            if "403" in str(e) or "429" in str(e):
                time.sleep(5)
            continue
    return new

def scrape_all_gamma() -> List[dict]:
    print("📡 Gamma API — scraping multi-stratégies")
    found: dict = {}
    strategies = [
        ("closed=true&order=endDate&ascending=false",  "fermés/récents",   80),
        ("closed=true&order=endDate&ascending=true",   "fermés/anciens",   80),
        ("closed=true&order=volume&ascending=false",   "fermés/volume",    30),
        ("active=false&order=endDate&ascending=false", "inactifs/récents", 50),
        ("order=endDate&ascending=false",              "tous/récents",     50),
        ("order=startDate&ascending=false",            "tous/startDate",   30),
    ]
    for params, label, max_pages in strategies:
        new = _scrape_strategy(params, label, found, max_pages)
        status = f"+{new} nouveaux (total={len(found)})" if new > 0 else "+0"
        print(f"  {'✅' if new else '─ '} {label:<32} {status}")

    markets  = list(found.values())
    resolved = [m for m in markets if m["resolved"]]
    print(f"\n  Résultat : {len(markets)} marchés température ({len(resolved)} résolus)\n")
    return markets

def fetch_real_prices(markets: List[dict], prices_map: Dict[str, dict]) -> Dict[str, dict]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    to_fetch = [
        m for m in markets
        if m.get("resolved") and m.get("yes_token_id") and m.get("end_date")
        and m["condition_id"] not in prices_map
        and m["end_date"] < today
        and m.get("source") != "huggingface"  # HF = AMM, pas de CLOB
        and float(m.get("volume") or 0) >= 100
    ]
    if not to_fetch:
        print("💰 Prix : tous déjà en cache.")
        return prices_map

    print(f"💰 Récupération vrais prix : {len(to_fetch)} marchés")
    for i, market in enumerate(to_fetch):
        cid      = market["condition_id"]
        token_id = market["yes_token_id"]
        end_date = market["end_date"]
        city = market.get('city') or market.get('question','')[:18]
        print(f"  [{i+1:>3}/{len(to_fetch)}] {city:<18} {end_date} ... ",
              end="", flush=True)
        try:
            target   = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            # Essaie 48h avant, puis 72h si vide
            for hours_back in [(48, 24), (72, 24), (96, 48)]:
                ts_start = int((target - timedelta(hours=hours_back[0])).timestamp())
                ts_end   = int((target - timedelta(hours=hours_back[1])).timestamp())
                url  = (f"{CLOB_URL}/prices-history?market={token_id}"
                        f"&startTs={ts_start}&endTs={ts_end}&fidelity=60")
                data   = _get(url, delay=0.4)
                points = data.get("history") or []
                if points:
                    break

            if not points:
                print("no history")
                prices_map[cid] = {"condition_id": cid, "yes_price": None, "data_points": 0}
                continue

            vals = [float(p["p"]) for p in points if "p" in p]
            avg  = sum(vals) / len(vals) if vals else 0

            if 0.03 < avg < 0.97:
                prices_map[cid] = {
                    "condition_id":  cid,
                    "yes_price":     round(avg, 4),
                    "yes_price_min": round(min(vals), 4),
                    "yes_price_max": round(max(vals), 4),
                    "data_points":   len(vals),
                    "fetched_at":    datetime.now(timezone.utc).isoformat(),
                }
                print(f"✅ YES={avg:.3f} ({len(vals)} pts)")
            else:
                print(f"déjà résolu ({avg:.3f})")
                prices_map[cid] = {"condition_id": cid, "yes_price": None, "data_points": 0}

        except Exception as e:
            print(f"⚠️ {e}")
            prices_map[cid] = {"condition_id": cid, "yes_price": None, "data_points": 0}

        # Sauvegarde intermédiaire toutes les 10 entrées
        if (i + 1) % 10 == 0:
            _save(PRICES_FILE, list(prices_map.values()))

    return prices_map

def print_summary(markets: List[dict], prices_map: Dict[str, dict]) -> None:
    from collections import Counter
    resolved   = [m for m in markets if m.get("resolved")]
    with_price = [m for m in resolved if prices_map.get(m["condition_id"], {}).get("yes_price")]
    active     = [m for m in markets if m.get("accepting")]
    print(f"\n{'═'*60}")
    print(f"  RÉSUMÉ HISTORIQUE")
    print(f"{'═'*60}")
    print(f"  Total marchés température   : {len(markets)}")
    print(f"  ├─ Résolus                  : {len(resolved)}")
    print(f"  ├─ Résolus + prix réel CLOB : {len(with_price)}  ← backtest fiable")
    print(f"  └─ Actifs (tradables)       : {len(active)}")
    if resolved:
        cities = Counter(m["city"] for m in resolved)
        print(f"\n  Top villes :")
        for city, count in cities.most_common(10):
            print(f"    {city:<22} {count:>3}")
        mods = Counter(m["modifier"] for m in resolved)
        print(f"\n  Types : " + "  ".join(f"{k}={v}" for k, v in mods.most_common()))
    print(f"\n  {'✅' if with_price else '⚠️ '} {len(with_price)} marchés prêts pour backtest")
    print(f"  ➜ python notebooks/backtest.py")
    print(f"{'═'*60}\n")

def run(prices_only: bool = False) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    existing_markets = _load(MARKETS_FILE)
    existing_prices  = _load(PRICES_FILE)
    all_markets = {m["condition_id"]: m for m in existing_markets}
    prices_map  = {p["condition_id"]: p for p in existing_prices}
    print(f"Cache existant : {len(existing_markets)} marchés, {len(existing_prices)} prix\n")

    if not prices_only:
        # Ingère depuis master_weather.json (HuggingFace + toutes sources)
        master_file = os.path.join(OUT_DIR, "master_weather.json")
        if os.path.exists(master_file):
            with open(master_file, encoding="utf-8") as f:
                master = json.load(f)
            added = 0
            for m in master:
                if not m.get("resolved") or not m.get("yes_token_id"):
                    continue
                cid = m.get("condition_id", "")
                if not cid or cid in all_markets:
                    continue
                all_markets[cid] = {
                    "condition_id": cid,
                    "question":     m.get("question", ""),
                    "end_date":     m.get("end_date", ""),
                    "resolved":     True,
                    "yes_won":      m.get("yes_won"),
                    "volume":       float(m.get("volume") or 0),
                    "yes_token_id": m.get("yes_token_id"),
                    "no_token_id":  m.get("no_token_id"),
                }
                added += 1
            print(f"  +{added} marchés ingérés depuis master_weather.json")
            _save(MARKETS_FILE, list(all_markets.values()))

        # Complète avec Gamma API (marchés récents)
        gamma_markets = scrape_all_gamma()
        new_count = 0
        for m in gamma_markets:
            cid = m["condition_id"]
            if not cid:
                continue
            if cid not in all_markets:
                all_markets[cid] = m
                new_count += 1
            elif m.get("resolved") and not all_markets[cid].get("resolved"):
                all_markets[cid].update(m)
        print(f"→ {new_count} nouveaux marchés Gamma ajoutés\n")
        _save(MARKETS_FILE, list(all_markets.values()))

    markets_list = list(all_markets.values())
    prices_map   = fetch_real_prices(markets_list, prices_map)
    _save(PRICES_FILE, list(prices_map.values()))
    print_summary(markets_list, prices_map)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prices", action="store_true",
                        help="Re-fetch les prix seulement")
    args = parser.parse_args()
    run(prices_only=args.prices)