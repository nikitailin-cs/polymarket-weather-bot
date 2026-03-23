"""
Récupère en masse la partie météo depuis Gamma API.

Objectif:
- récupérer un corpus large de marchés météo (actifs + fermés/résolus)
- conserver question + règles de résolution + source + outcome
- exporter en JSON/CSV pour tests regex/LLM offline

Usage:
  python temp/fetch_all_weather_markets.py
  python temp/fetch_all_weather_markets.py --max-pages 200 --limit 1000
  python temp/fetch_all_weather_markets.py --out-prefix data/raw/weather_markets_bulk
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any


GAMMA_URL = "https://gamma-api.polymarket.com/markets"

WEATHER_MARKERS = (
    "temperature", "highest temperature", "lowest temperature",
    "fahrenheit", "celsius", "precipitation", "rain", "rainfall", "snow", "snowfall",
    "hurricane", "tropical storm", "typhoon", "cyclone", "tornado",
    "wind speed", "humidity", "heatwave",
)


def _get_json(url: str, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _parse_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _first_non_empty_str(d: dict, keys: list[str]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _extract_rules_text(raw: dict) -> str:
    keys = [
        "resolutionRules", "resolution_rules", "resolutionCriteria",
        "description", "rules", "details", "clarification",
    ]
    parts: list[str] = []
    for k in keys:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    meta = raw.get("metadata")
    if isinstance(meta, dict):
        for k in keys:
            v = meta.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
    return "\n\n".join(parts)


def _weather_source_from_text(text: str) -> str:
    t = (text or "").lower()
    if "wunderground" in t:
        return "wunderground"
    if "weather.com" in t:
        return "weather_com"
    if "accuweather" in t:
        return "accuweather"
    if "noaa" in t or "weather.gov" in t or "nws" in t:
        return "noaa_ghcnd"
    if "nhc" in t or "national hurricane center" in t:
        return "nhc"
    if "meteo-france" in t or "meteo france" in t:
        return "meteo_france"
    if "copernicus" in t:
        return "copernicus"
    if "wmo" in t:
        return "wmo_station"
    return "unknown"


def _is_weather_market(question: str, rules_text: str, resolution_source: str) -> bool:
    blob = " ".join([question or "", rules_text or "", resolution_source or ""]).lower()
    if any(k in blob for k in WEATHER_MARKERS):
        return True
    # motif météo structuré (temp with unit)
    if re.search(r"\b\d+(?:\.\d+)?\s*[°º]?\s*(c|f|celsius|fahrenheit)\b", blob):
        return True
    # source météo reconnue dans URL/rules
    if any(x in blob for x in ("wunderground", "weather.gov.hk", "giss.nasa.gov", "accuweather", "weather.com")):
        return True
    return False


def _infer_yes_won(raw: dict) -> Any:
    outcomes = [str(x).strip().lower() for x in _parse_list(raw.get("outcomes"))]
    prices = []
    for x in _parse_list(raw.get("outcomePrices")):
        try:
            prices.append(float(x))
        except Exception:
            pass
    if not outcomes or not prices or len(prices) < len(outcomes):
        return None
    if max(prices) < 0.95:
        return None
    winner_idx = prices.index(max(prices))
    if winner_idx >= len(outcomes):
        return None
    return outcomes[winner_idx] == "yes"


def _to_record(raw: dict) -> dict:
    question = str(raw.get("question") or "")
    cid = str(raw.get("conditionId") or raw.get("condition_id") or "")
    rid = str(raw.get("id") or "")
    rules_text = _extract_rules_text(raw)
    resolution_source = _first_non_empty_str(raw, ["resolutionSource", "resolution_source"])
    source_blob = " ".join([resolution_source, rules_text])
    source_guess = _weather_source_from_text(source_blob)

    prices = _parse_list(raw.get("outcomePrices"))
    try:
        prices = [float(x) for x in prices]
    except Exception:
        prices = []

    return {
        "id": rid,
        "condition_id": cid,
        "question": question,
        "end_date": str(raw.get("endDate") or raw.get("endDateIso") or ""),
        "active": bool(raw.get("active", False)),
        "closed": bool(raw.get("closed", False)),
        "accepting_orders": bool(raw.get("acceptingOrders", False)),
        "volume": float(raw.get("volume") or 0.0),
        "liquidity": float(raw.get("liquidityNum") or raw.get("liquidity") or 0.0),
        "resolution_source_field": resolution_source,
        "rules_text": rules_text,
        "weather_source_guess": source_guess,
        "outcomes": _parse_list(raw.get("outcomes")),
        "outcome_prices": prices,
        "yes_won": _infer_yes_won(raw),
        "event_slug": str(raw.get("eventSlug") or raw.get("event_slug") or ""),
        "event_title": str(raw.get("eventTitle") or raw.get("event_title") or ""),
    }


def _fetch_strategy(params: dict[str, Any], max_pages: int, page_limit: int) -> list[dict]:
    rows: list[dict] = []
    for page in range(max_pages):
        offset = page * page_limit
        q = dict(params)
        q["limit"] = page_limit
        q["offset"] = offset
        url = f"{GAMMA_URL}?{urllib.parse.urlencode(q)}"
        try:
            batch = _get_json(url)
        except Exception as e:
            print(f"  ⚠️ fetch fail page={page} offset={offset}: {e}")
            time.sleep(1.0)
            continue
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        if len(batch) < page_limit:
            break
        time.sleep(0.12)
    return rows


def fetch_all_weather(max_pages: int, page_limit: int) -> list[dict]:
    strategies = [
        {"active": "true", "closed": "false", "order": "volume", "ascending": "false"},
        {"active": "false", "closed": "true", "order": "endDate", "ascending": "false"},
        {"active": "false", "closed": "true", "order": "endDate", "ascending": "true"},
        {"closed": "true", "order": "volume", "ascending": "false"},
        {"order": "endDate", "ascending": "false"},
    ]
    dedup: dict[str, dict] = {}
    for i, params in enumerate(strategies, start=1):
        print(f"\n[{i}/{len(strategies)}] strategy={params}")
        raw_rows = _fetch_strategy(params, max_pages=max_pages, page_limit=page_limit)
        print(f"  fetched raw: {len(raw_rows)}")
        kept = 0
        for raw in raw_rows:
            rec = _to_record(raw)
            if not _is_weather_market(rec["question"], rec["rules_text"], rec["resolution_source_field"]):
                continue
            key = rec["condition_id"] or rec["id"]
            if not key:
                continue
            if key not in dedup:
                dedup[key] = rec
                kept += 1
            else:
                # merge léger: garde rules/source non vides si absents
                old = dedup[key]
                if not old.get("rules_text") and rec.get("rules_text"):
                    old["rules_text"] = rec["rules_text"]
                if not old.get("resolution_source_field") and rec.get("resolution_source_field"):
                    old["resolution_source_field"] = rec["resolution_source_field"]
                if old.get("weather_source_guess") == "unknown" and rec.get("weather_source_guess") != "unknown":
                    old["weather_source_guess"] = rec["weather_source_guess"]
        print(f"  kept weather new: {kept} | total weather unique: {len(dedup)}")
    return list(dedup.values())


def write_outputs(records: list[dict], out_prefix: str) -> tuple[str, str]:
    json_path = out_prefix + ".json"
    csv_path = out_prefix + ".csv"
    out_dir = os.path.dirname(json_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    fields = [
        "id", "condition_id", "question", "end_date", "active", "closed",
        "volume", "liquidity", "resolution_source_field", "weather_source_guess",
        "yes_won", "event_slug", "event_title", "rules_text",
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k) for k in fields})
    return json_path, csv_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk fetch all weather markets from Gamma")
    parser.add_argument("--max-pages", type=int, default=120, help="Pages max par stratégie")
    parser.add_argument("--limit", type=int, default=500, help="Taille de page Gamma")
    parser.add_argument("--out-prefix", default="data/raw/weather_markets_bulk", help="Prefix sortie sans extension")
    args = parser.parse_args()

    print("Fetching weather corpus from Gamma...")
    records = fetch_all_weather(max_pages=args.max_pages, page_limit=args.limit)
    records.sort(key=lambda r: (r.get("end_date") or "", r.get("condition_id") or ""))

    with_rules = sum(1 for r in records if r.get("rules_text"))
    with_source = sum(1 for r in records if r.get("resolution_source_field"))
    resolved = sum(1 for r in records if r.get("closed"))
    print(f"\nTotal weather markets: {len(records)}")
    print(f"  with rules_text: {with_rules}")
    print(f"  with resolution_source_field: {with_source}")
    print(f"  closed/resolved candidates: {resolved}")

    jp, cp = write_outputs(records, args.out_prefix)
    print(f"\nJSON: {jp}")
    print(f"CSV : {cp}")


if __name__ == "__main__":
    main()
