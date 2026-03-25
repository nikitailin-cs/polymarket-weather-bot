#!/usr/bin/env python3
"""
Compare ``data/active_markets.json`` (temp uniquement, ICAO valide) avec ``data/icao_mapping.json``.

Signale les marchés actifs sans entrée RT et les clés mapping sans marché actif correspondant
(approximatif : par market_id seulement).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_LT = _ROOT / "bots" / "bot1" / "Long Term" / "scripts"
if str(_LT) not in sys.path:
    sys.path.insert(0, str(_LT))

import market_manager as mm  # noqa: E402
from monitoring.error_guard import run_guarded  # noqa: E402


def _rt_eligible(m: mm.ActiveMarket) -> bool:
    var = (m.variable or "").lower()
    if var not in ("temp_max", "temp_min", "temp_avg"):
        return False
    code = (m.station_code or "").strip().upper()
    if mm._looks_like_wmo(code):
        return False
    if not mm._looks_like_icao_or_faa(code):
        return False
    if not (m.locality_tz or "").strip():
        return False
    if not (m.target_date or "").strip() and not (
        (m.target_date_start or "").strip() and (m.target_date_end or "").strip()
    ):
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Écart active_markets ↔ icao_mapping (bot2)")
    ap.add_argument("--active", type=Path, default=mm.default_markets_json_path())
    ap.add_argument("--mapping", type=Path, default=_ROOT / "data" / "icao_mapping.json")
    args = ap.parse_args()

    markets = mm.load_active_markets(args.active)
    active_ids = {m.market_id.strip() for m in markets if _rt_eligible(m) and (m.market_id or "").strip()}

    mapping: dict[str, Any] = {}
    if args.mapping.is_file():
        raw = json.loads(args.mapping.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            mapping = raw

    map_ids = {k for k in mapping if isinstance(k, str) and not k.startswith("_")}

    missing_in_mapping = sorted(active_ids - map_ids)
    extra_in_mapping = sorted(map_ids - active_ids)

    print(f"Marchés actifs RT-éligibles: {len(active_ids)}", flush=True)
    print(f"Clés icao_mapping (hors _): {len(map_ids)}", flush=True)
    print(f"Dans actifs mais pas mapping: {len(missing_in_mapping)}", flush=True)
    for mid in missing_in_mapping[:50]:
        print(f"  - {mid}", flush=True)
    if len(missing_in_mapping) > 50:
        print(f"  ... +{len(missing_in_mapping) - 50} autres", flush=True)

    print(f"Dans mapping mais pas actifs RT-éligibles: {len(extra_in_mapping)}", flush=True)
    for mid in extra_in_mapping[:50]:
        print(f"  - {mid}", flush=True)
    if len(extra_in_mapping) > 50:
        print(f"  ... +{len(extra_in_mapping) - 50} autres", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(run_guarded("bot2.rt_mapping_gap_report", main))
