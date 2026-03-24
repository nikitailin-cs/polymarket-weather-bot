#!/usr/bin/env python3
"""
Adapter Scanner -> JSON (registre bot1).

Objectif:
  - appeler le pipeline existant `scan_weather_markets`
  - récupérer le ContractSpec de chaque marché
  - sauvegarder une liste épurée dans `data/active_markets.json`

Ce script est prévu pour être lancé périodiquement (cron, ex. toutes les 4h).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    # .../POLYMARKET/bots/bot1/Long Term/scripts/generate_markets.py -> .../POLYMARKET
    return Path(__file__).resolve().parents[4]


ROOT = _repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import CONTRACT_SPECS_FILE  # noqa: E402
from core.models import ContractSpecCache  # noqa: E402
from scanners.market_scanner import scan_weather_markets  # noqa: E402


DEFAULT_OUTPUT_JSON = ROOT / "data" / "active_markets.json"


def _to_market_record(market: Any, spec: Any) -> dict[str, Any]:
    oracle_source = str(getattr(spec, "oracle_source", "") or "unknown").strip().lower()
    return {
        # Identifiants
        "market_id": market.condition_id,
        "condition_id": market.condition_id,
        # Clé de jointure station (station = nom canonique ; icao conservé en compat)
        "station": spec.oracle_station_id,
        "icao": spec.oracle_station_id,
        # Condition mathématique normalisée depuis ContractSpec
        "variable": spec.weather_variable,
        "operator": spec.threshold_op,
        "threshold": spec.threshold_value,
        "threshold_range": spec.threshold_range,
        "unit": spec.threshold_unit,
        "oracle_rounding": spec.oracle_rounding,
        # Métadonnées utiles audit/debug
        "oracle_source": oracle_source,
        "oracle_alignment_score": spec.oracle_alignment_score,
        "question": market.question,
    }


def generate_active_markets_json(output_json: Path, limit: int) -> tuple[int, int, int]:
    spec_cache = ContractSpecCache(CONTRACT_SPECS_FILE)
    markets = scan_weather_markets(limit=limit, spec_cache=spec_cache)

    kept: list[dict[str, Any]] = []
    missing_spec = 0
    missing_station = 0

    for m in markets:
        spec = spec_cache.get(m.condition_id)
        if spec is None:
            missing_spec += 1
            print(f"[WARN] spec introuvable pour market_id={m.condition_id}", flush=True)
            continue
        if not spec.oracle_station_id:
            missing_station += 1
            print(
                f"[WARN] station vide pour market_id={m.condition_id} question={m.question[:90]!r}",
                flush=True,
            )
            continue
        kept.append(_to_market_record(m, spec))

    payload = {
        "markets": kept,
        "meta": {
            "scanner_limit": limit,
            "total_scanned_weather_markets": len(markets),
            "kept_markets": len(kept),
            "skipped_missing_spec": missing_spec,
            "skipped_missing_station": missing_station,
            "contract_specs_file": CONTRACT_SPECS_FILE,
        },
    }

    output_json.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_json.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(output_json)

    return len(markets), len(kept), missing_station + missing_spec


def main() -> int:
    parser = argparse.ArgumentParser(description="Génère data/active_markets.json depuis le scanner live.")
    parser.add_argument(
        "--output-json",
        type=str,
        default=str(DEFAULT_OUTPUT_JSON),
        help=f"Chemin du registre JSON (défaut: {DEFAULT_OUTPUT_JSON})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Limite de marchés demandés à Gamma API (défaut: 500).",
    )
    args = parser.parse_args()

    out = Path(args.output_json).expanduser().resolve()
    try:
        scanned, kept, skipped = generate_active_markets_json(out, max(1, int(args.limit)))
    except Exception as exc:
        print(f"[ERREUR] generate_markets: {exc}", file=sys.stderr)
        return 1

    print(f"[OK] JSON généré: {out}")
    print(f"[OK] marchés météo scannés: {scanned}")
    print(f"[OK] marchés conservés: {kept}")
    print(f"[OK] marchés ignorés: {skipped}")
    print("[INFO] Cron recommandé: toutes les 4h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
