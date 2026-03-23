"""
Affiche des exemples lisibles en CMD pour vérifier les champs:
- instructions / resolution rules
- resolution source
- outcome de résolution

Usage:
  python temp/show_dataset_examples.py
  python temp/show_dataset_examples.py --n 3
  python temp/show_dataset_examples.py --weather-only
"""
from __future__ import annotations

import argparse
import json
import os
import textwrap
from typing import Any


ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA_RAW = os.path.join(ROOT, "data", "raw")

HF_PARQUET = os.path.join(DATA_RAW, "hf", "markets.parquet")
HF_WEATHER = os.path.join(DATA_RAW, "hf", "weather_complete.json")
HIST_MARKETS = os.path.join(DATA_RAW, "historical_markets.json")
MASTER_WEATHER = os.path.join(DATA_RAW, "master_weather.json")


def _wrap(text: Any, width: int = 110) -> str:
    s = str(text or "").strip()
    if not s:
        return "<vide>"
    lines = []
    for ln in s.splitlines():
        ln = ln.strip()
        if not ln:
            lines.append("")
            continue
        lines.extend(textwrap.wrap(ln, width=width, break_long_words=False))
    return "\n".join(lines)


def _first_non_empty(d: dict, keys: list[str]) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _load_json(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _is_weather_text(q: str) -> bool:
    s = (q or "").lower()
    markers = (
        "temperature", "precipitation", "rain", "snow", "hurricane",
        "wind speed", "humidity",
    )
    return any(m in s for m in markers)


def _print_rows(title: str, rows: list[dict], n: int, weather_only: bool = False) -> None:
    print(f"\n{'='*120}")
    print(title)
    print(f"{'='*120}")
    shown = 0
    for row in rows:
        q = str(_first_non_empty(row, ["question", "title", "market_question"]) or "")
        if weather_only and not _is_weather_text(q):
            continue
        raw = row.get("_raw_market") if isinstance(row.get("_raw_market"), dict) else {}

        rules = _first_non_empty(
            row,
            ["resolutionRules", "resolution_rules", "resolutionCriteria", "description", "rules", "details"],
        )
        if rules in (None, "", [], {}):
            rules = _first_non_empty(
                raw,
                ["resolutionRules", "resolution_rules", "resolutionCriteria", "description", "rules", "details"],
            )

        resolution_source = _first_non_empty(row, ["resolutionSource", "resolution_source"])
        if resolution_source in (None, "", [], {}):
            resolution_source = _first_non_empty(raw, ["resolutionSource", "resolution_source"])

        resolved = _first_non_empty(row, ["resolved", "closed", "active"])
        yes_won = _first_non_empty(row, ["yes_won", "outcome", "winner"])
        cid = _first_non_empty(row, ["condition_id", "conditionId", "id"])

        print("-" * 120)
        print(f"condition_id: {cid or '<inconnu>'}")
        print(f"question: {_wrap(q)}")
        print(f"resolutionSource: {_wrap(resolution_source)}")
        print("instructions/rules:")
        print(_wrap(rules))
        print(f"resolved/closed: {resolved}")
        print(f"outcome(yes_won/winner): {yes_won}")
        shown += 1
        if shown >= n:
            break
    if shown == 0:
        print("Aucun exemple affichable avec ce filtre.")


def _load_parquet_rows(path: str, limit: int = 50) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        import pandas as pd  # lazy import
    except Exception:
        print("⚠️ pandas indisponible, skip parquet.")
        return []
    try:
        df = pd.read_parquet(path)
        # garde une fenêtre pour rester rapide
        return df.head(limit).to_dict(orient="records")
    except Exception as e:
        print(f"⚠️ lecture parquet impossible: {e}")
        return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Affiche des exemples de datasets en CMD.")
    parser.add_argument("--n", type=int, default=5, help="Nombre d'exemples par dataset")
    parser.add_argument("--weather-only", action="store_true", help="Filtre aux marchés météo")
    args = parser.parse_args()

    print("Datasets détectés:")
    for p in (HF_PARQUET, HF_WEATHER, HIST_MARKETS, MASTER_WEATHER):
        print(f"  {'✅' if os.path.exists(p) else '❌'} {p}")

    parquet_rows = _load_parquet_rows(HF_PARQUET, limit=max(100, args.n * 10))
    _print_rows("HF markets.parquet (échantillon)", parquet_rows, args.n, weather_only=args.weather_only)

    weather_rows = _load_json(HF_WEATHER)
    _print_rows("HF weather_complete.json", weather_rows, args.n, weather_only=args.weather_only)

    hist_rows = _load_json(HIST_MARKETS)
    _print_rows("historical_markets.json", hist_rows, args.n, weather_only=args.weather_only)

    master_rows = _load_json(MASTER_WEATHER)
    _print_rows("master_weather.json", master_rows, args.n, weather_only=args.weather_only)


if __name__ == "__main__":
    main()
