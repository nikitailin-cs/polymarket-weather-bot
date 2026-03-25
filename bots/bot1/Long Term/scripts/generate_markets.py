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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _repo_root() -> Path:
    # .../POLYMARKET/bots/bot1/Long Term/scripts/generate_markets.py -> .../POLYMARKET
    return Path(__file__).resolve().parents[4]


ROOT = _repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import CONTRACT_SPECS_FILE  # noqa: E402
from core.contract_dates import infer_target_date_yyyy_mm_dd  # noqa: E402
from core.models import ContractSpecCache  # noqa: E402
from core.station_timezone import resolve_iana_timezone  # noqa: E402
from scanners.market_scanner import scan_weather_markets  # noqa: E402


DEFAULT_OUTPUT_JSON = ROOT / "data" / "active_markets.json"


def _fmt_end_utc_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_market_record(market: Any, spec: Any) -> dict[str, Any]:
    oracle_source = str(getattr(spec, "oracle_source", "") or "unknown").strip().lower()
    end_iso = str(getattr(spec, "end_date_iso", "") or getattr(market, "end_date_iso_raw", "") or "")
    target_date = infer_target_date_yyyy_mm_dd(
        question=market.question,
        end_date_iso=end_iso,
        market_end_utc=market.end_date,
    )
    locality_tz = resolve_iana_timezone(spec.oracle_station_id)
    gamma_raw = getattr(market, "gamma_raw", None) or {}
    tokens_payload = [
        {"outcome": t.outcome, "token_id": t.token_id, "price": t.price}
        for t in getattr(market, "tokens", []) or []
    ]
    rec: dict[str, Any] = {
        # Identifiants
        "market_id": market.condition_id,
        "condition_id": market.condition_id,
        "slug": getattr(market, "slug", "") or None,
        "gamma_id": getattr(market, "gamma_id", "") or None,
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
        "city": getattr(spec, "city", "") or "",
        "country": getattr(spec, "country", "") or "",
        "resolution_source_contract": getattr(spec, "resolution_source", "") or "",
        "resolution_source_gamma": getattr(market, "resolution_source", "") or "",
        "end_date_iso": end_iso or None,
        "end_date_utc_iso": _fmt_end_utc_iso(market.end_date),
        "target_date": target_date,
        "timezone": locality_tz,
        "liquidity_usd": float(getattr(market, "liquidity_usd", 0.0) or 0.0),
        "volume": float(getattr(market, "volume", 0.0) or 0.0),
        "tokens": tokens_payload,
        "gamma": gamma_raw,
    }
    # JSON propre : pas de clés null inutiles pour slug/gamma_id
    if rec.get("slug") is None:
        rec.pop("slug", None)
    if rec.get("gamma_id") in (None, ""):
        rec.pop("gamma_id", None)
    return rec


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
