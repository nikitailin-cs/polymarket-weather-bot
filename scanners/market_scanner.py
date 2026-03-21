"""
Scanner marchés météo via Gamma API Polymarket.
- 1 seule requête HTTP (pas de pagination infinie)
- Filtre météo par regex word-boundary
- Retourne des WeatherMarket typés
"""
from __future__ import annotations
import re, sys, os, json, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone
from typing import List, Optional, Any
from core.models import WeatherMarket, TokenInfo
from config.keywords import WEATHER_KEYWORDS

GAMMA_URL   = "https://gamma-api.polymarket.com/markets"
GAMMA_LIMIT = int(os.getenv("GAMMA_LIMIT", "500"))

def _match_keyword(text: str) -> Optional[str]:
    s = (text or "").lower()
    for kw in WEATHER_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", s):
            return kw
    return None

def _parse_dt(v: Any) -> Optional[datetime]:
    if not v: return None
    try: return datetime.strptime(str(v)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError: return None

def _fetch_gamma(limit: int = GAMMA_LIMIT) -> List[dict]:
    url = f"{GAMMA_URL}?active=true&closed=false&limit={limit}&order=volume&ascending=false"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def _build_market(raw: dict) -> Optional[WeatherMarket]:
    if not raw.get("enableOrderBook"): return None
    if not raw.get("acceptingOrders"): return None
    end_date = _parse_dt(raw.get("endDate") or raw.get("endDateIso"))
    now = datetime.now(timezone.utc)
    if end_date and (end_date - now).total_seconds() < 86400: return None
    question = raw.get("question") or ""
    keyword = _match_keyword(question)
    if keyword is None: return None
    volume = float(raw.get("volume") or 0)

    # Parser les champs qui peuvent être des strings JSON
    def _parse_list(v):
        if isinstance(v, list): return v
        if isinstance(v, str):
            try: return json.loads(v)
            except: return []
        return []

    token_ids = _parse_list(raw.get("clobTokenIds"))
    outcomes  = _parse_list(raw.get("outcomes")) or ["Yes", "No"]
    try: prices = [float(p) for p in _parse_list(raw.get("outcomePrices"))]
    except: prices = [0.5] * len(outcomes)

    while len(prices) < len(outcomes): prices.append(0.5)
    while len(token_ids) < len(outcomes): token_ids.append("")

    tokens = [
        TokenInfo(token_id=token_ids[i], outcome=outcomes[i], price=prices[i])
        for i in range(len(outcomes))
    ]
    return WeatherMarket(
        condition_id=raw.get("conditionId") or "",
        question=question, tokens=tokens, end_date=end_date,
        volume=volume, active=True, accepting_orders=True,
        enable_order_book=True, matched_keyword=keyword,
    )

def scan_weather_markets(limit: int = GAMMA_LIMIT) -> List[WeatherMarket]:
    print(f"🔍 Gamma API scan | limit={limit}")
    raw_markets = _fetch_gamma(limit)
    print(f"   Reçus : {len(raw_markets)} marchés")
    results = [m for raw in raw_markets if (m := _build_market(raw))]
    results.sort(key=lambda m: m.volume, reverse=True)
    print(f"   ✅ Météo actifs : {len(results)}")
    return results

def print_markets(markets: List[WeatherMarket]) -> None:
    if not markets:
        print("\n⚠️  Aucun marché météo actif."); return
    print(f"\n{'═'*72}")
    for i, m in enumerate(markets, 1):
        days  = m.days_to_expiry
        yes_p = m.market_yes_prob
        no_p  = 1 - yes_p if yes_p is not None else None
        print(f"\n#{i:02d} {m.question}")
        print(f"     Expire : {f'{days:.1f}j' if days else '?':8} Volume : ${m.volume:>10,.0f}   Keyword : '{m.matched_keyword}'")
        print(f"     YES={f'{yes_p*100:.1f}%' if yes_p else '?'}  NO={f'{no_p*100:.1f}%' if no_p else '?'}")
        print(f"     condition_id : {m.condition_id}")
        for t in m.tokens:
            print(f"       [{t.outcome:3s}] price={t.price:.4f}   token_id={t.token_id[:28]}...")
    print(f"\n{'═'*72}\nTotal : {len(markets)} marchés météo actifs\n")

def debug_lost(limit: int = GAMMA_LIMIT) -> None:
    """Affiche les marchés rejetés par match_keyword pour identifier les perdus."""
    from config.keywords import WEATHER_KEYWORDS
    import re
    
    raw_markets = _fetch_gamma(limit)
    for raw in raw_markets:
        if not raw.get("enableOrderBook"): continue
        if not raw.get("acceptingOrders"): continue
        q = raw.get("question") or ""
        # A un keyword brut mais rejeté par match_keyword
        s = q.lower()
        has_raw_kw = any(re.search(r"\b" + re.escape(kw) + r"\b", s) for kw in WEATHER_KEYWORDS)
        has_match = _match_keyword(q) is not None
        if has_raw_kw and not has_match:
            vol = float(raw.get("volume") or 0)
            print(f"  REJETÉ | vol=${vol:,.0f} | {q}")

if __name__ == "__main__":
    debug_lost()      # ← temporaire
    markets = scan_weather_markets()
    print_markets(markets)