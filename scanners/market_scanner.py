"""
Scanner marchés météo via Gamma API Polymarket.
- 1 seule requête HTTP (pas de pagination infinie)
- Filtre météo par regex word-boundary
- Retourne des WeatherMarket typés
- v2 : parse ContractSpec sur chaque nouveau marché (1 fois, mis en cache)
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

def _build_market(raw: dict) -> Optional[tuple]:
    if not raw.get("enableOrderBook"):  return None
    if not raw.get("acceptingOrders"):  return None
    end_date = _parse_dt(raw.get("endDate") or raw.get("endDateIso"))
    now = datetime.now(timezone.utc)
    if end_date and (end_date - now).total_seconds() < 86400: return None
    question = raw.get("question") or ""
    keyword  = _match_keyword(question)
    if keyword is None: return None
    volume = float(raw.get("volume") or 0)

    def _parse_list(v):
        if isinstance(v, list): return v
        if isinstance(v, str):
            try: return json.loads(v)
            except: return []
        return []

    token_ids = _parse_list(raw.get("clobTokenIds"))
    outcomes  = _parse_list(raw.get("outcomes")) or ["Yes", "No"]
    try:    prices = [float(p) for p in _parse_list(raw.get("outcomePrices"))]
    except: prices = [0.5] * len(outcomes)

    while len(prices)    < len(outcomes): prices.append(0.5)
    while len(token_ids) < len(outcomes): token_ids.append("")

    tokens = [
        TokenInfo(token_id=token_ids[i], outcome=outcomes[i], price=prices[i])
        for i in range(len(outcomes))
    ]
    return WeatherMarket(
        condition_id      = raw.get("conditionId") or "",
        question          = question,
        tokens            = tokens,
        end_date          = end_date,
        volume            = volume,
        active            = True,
        accepting_orders  = True,
        enable_order_book = True,
        matched_keyword   = keyword,
    ), raw.get("resolutionSource", ""), float(raw.get("liquidityNum") or raw.get("liquidity") or volume or 0)


def scan_weather_markets(
    limit:      int                         = GAMMA_LIMIT,
    spec_cache: "Optional[ContractSpecCache]" = None,   # type: ignore[name-defined]
) -> List[WeatherMarket]:
    """
    Scanne les marchés météo actifs.

    Si spec_cache est fourni, parse le ContractSpec de chaque nouveau marché
    (un seul appel LLM/regex par marché, résultat mis en cache).
    Les marchés dont le score d'alignement est trop faible sont éliminés
    UNIQUEMENT si spec_cache est actif — rétrocompatibilité garantie sinon.
    """
    print(f"🔍 Gamma API scan | limit={limit}")
    raw_markets = _fetch_gamma(limit)
    print(f"   Reçus : {len(raw_markets)} marchés")

    results: List[WeatherMarket] = []

    for raw in raw_markets:
        result = _build_market(raw)
        if result is None:
            continue
        market, resolution_source, liquidity_usd = result

        # ── Filtre liquidité minimal ──────────────────────────────────────────
        # En dessous de MIN_LIQUIDITY_USD, le spread réel dépasse notre
        # estimation de 2.5% → edge calculé est fictif → on écarte.
        from config.settings import MIN_LIQUIDITY_USD
        if liquidity_usd < MIN_LIQUIDITY_USD and market.volume < MIN_LIQUIDITY_USD:
            continue

        # ── Parsing contractuel (v2, optionnel) ──────────────────────────────
        if spec_cache is not None:
            from core.contract_parser import parse_contract
            from config.settings import MIN_ORACLE_ALIGNMENT

            # Récupère ou crée le ContractSpec (0 réseau si déjà en cache)
            end_iso = raw.get("endDate") or raw.get("endDateIso") or ""
            spec = parse_contract(
                market_id          = market.condition_id,
                condition_id       = market.condition_id,
                question           = market.question,
                end_date_iso       = str(end_iso),
                cache              = spec_cache,
                resolution_source  = resolution_source,
                liquidity_usd      = liquidity_usd,
            )

            # Marché écarté : parsing insuffisant ou oracle inconnu
            if spec is None:
                continue

            # Marché non tradeable (déjà fermé / résolu côté cache)
            if not spec.is_tradeable:
                continue

        results.append(market)

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
    raw_markets = _fetch_gamma(limit)
    for raw in raw_markets:
        if not raw.get("enableOrderBook"): continue
        if not raw.get("acceptingOrders"): continue
        q = raw.get("question") or ""
        s = q.lower()
        has_raw_kw = any(re.search(r"\b" + re.escape(kw) + r"\b", s) for kw in WEATHER_KEYWORDS)
        has_match  = _match_keyword(q) is not None
        if has_raw_kw and not has_match:
            vol = float(raw.get("volume") or 0)
            print(f"  REJETÉ | vol=${vol:,.0f} | {q}")


if __name__ == "__main__":
    debug_lost()
    markets = scan_weather_markets()
    print_markets(markets)