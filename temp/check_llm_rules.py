"""
Diagnostic dédié du fallback LLM pour l'extraction de source depuis les rules.

Usage:
  python temp/check_llm_rules.py --input-json data/raw/weather_markets_bulk.json
  python temp/check_llm_rules.py --input-json data/raw/weather_markets_bulk.json --limit 30 --only-unknown
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    # Si python-dotenv n'est pas dispo, on continue avec l'environnement système.
    pass

from core.contract_parser import _llm_extract_source_from_rules_with_diag  # noqa: E402


def _is_weather_row(question: str, rules_text: str, source: str) -> bool:
    blob = " ".join([question or "", rules_text or "", source or ""]).lower()
    markers = (
        "temperature", "global temperature", "fahrenheit", "celsius",
        "precipitation", "rainfall", "snowfall",
        "hurricane", "tropical storm", "typhoon", "cyclone",
        "wind speed", "humidity", "heatwave",
    )
    if any(m in blob for m in markers):
        return True
    if any(x in blob for x in ("wunderground", "weather.gov.hk", "giss.nasa.gov", "accuweather", "weather.com")):
        return True
    return False


def _load_rows(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        return []
    rows = []
    for r in data:
        if not isinstance(r, dict):
            continue
        rows.append(
            {
                "id": str(r.get("condition_id") or r.get("conditionId") or r.get("id") or ""),
                "question": str(r.get("question") or ""),
                "rules_text": str(r.get("rules_text") or r.get("resolutionRules") or r.get("description") or ""),
                "resolution_source_field": str(
                    r.get("resolution_source_field") or r.get("resolutionSource") or ""
                ),
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnostic LLM source extractor")
    parser.add_argument("--input-json", required=True, help="Dataset JSON (ex: data/raw/weather_markets_bulk.json)")
    parser.add_argument("--limit", type=int, default=50, help="Nombre d'entrées à tester")
    parser.add_argument("--only-unknown", action="store_true", help="Tester seulement les entrées sans source field")
    parser.add_argument("--show-rules", action="store_true", help="Afficher le rules_text complet")
    parser.add_argument("--weather-only", action="store_true", help="Filtre strict météo avant diagnostic")
    args = parser.parse_args()

    rows = _load_rows(args.input_json)
    if args.only_unknown:
        rows = [r for r in rows if not r["resolution_source_field"].strip()]
    if args.weather_only:
        rows = [r for r in rows if _is_weather_row(r["question"], r["rules_text"], r["resolution_source_field"])]
    rows = rows[: args.limit]

    print(f"Input rows: {len(rows)}")
    print(f"ANTHROPIC_API_KEY set: {bool(os.getenv('ANTHROPIC_API_KEY'))}")
    print("Tip: set RULES_LLM_DEBUG=1 for detailed internal logs.\n")

    src_counter: Counter[str] = Counter()
    mode_counter: Counter[str] = Counter()
    conf_vals: list[float] = []
    unknown_count = 0
    error_count = 0

    for i, r in enumerate(rows, start=1):
        try:
            src, conf, url, mode, reason = _llm_extract_source_from_rules_with_diag(
                rules_text=r["rules_text"],
                gamma_resolution_source=r["resolution_source_field"],
            )
        except Exception as e:
            error_count += 1
            src, conf, url = "unknown", 0.0, ""
            print("-" * 100)
            print(f"[{i}/{len(rows)}] id={r['id'] or 'N/A'}")
            print(f"question: {r['question']}")
            print(f"ERROR: {e}")
            continue
        src_counter[src] += 1
        mode_counter[f"{mode}:{reason}"] += 1
        conf_vals.append(conf)
        if src == "unknown":
            unknown_count += 1

        print("-" * 100)
        print(f"[{i}/{len(rows)}] id={r['id'] or 'N/A'}")
        print(f"question: {r['question']}")
        print(f"gamma_resolution_source: {r['resolution_source_field'] or '<vide>'}")
        print(f"llm_source: {src} | conf={conf:.3f} | url={url or '<vide>'}")
        print(f"llm_mode: {mode} | reason: {reason}")
        if args.show_rules:
            print("rules_text:")
            print(r["rules_text"] or "<vide>")

    avg_conf = (sum(conf_vals) / len(conf_vals)) if conf_vals else 0.0
    print("\n" + "=" * 100)
    print("SUMMARY")
    print(f"rows_tested: {len(rows)}")
    print(f"unknown_count: {unknown_count}")
    print(f"error_count: {error_count}")
    print(f"unknown_rate: {(unknown_count / len(rows) * 100):.1f}% " if rows else "unknown_rate: N/A")
    print(f"avg_confidence: {avg_conf:.3f}")
    print("source_distribution:")
    for s, c in src_counter.most_common():
        print(f"  - {s}: {c}")
    print("mode_distribution:")
    for m, c in mode_counter.most_common():
        print(f"  - {m}: {c}")
    print("=" * 100)


if __name__ == "__main__":
    main()
