#!/usr/bin/env python3
"""
Met à jour ``data/icao_mapping.json`` à partir de ``data/active_markets.json`` pour le bot2 RT.

- Ne traite que les variables ``temp_max`` / ``temp_min`` / ``temp_avg``.
- Ignore les stations WMO purement numériques (pas d’OACI METAR AWC dans ce flux).
- Fusion : les entrées existantes dont l’id n’est pas dans le registre actif sont conservées.
  Les marchés présents dans ``active_markets`` **écrasent** la clé correspondante.

Écriture de la sortie : **atomique** (fichier temporaire puis ``os.replace``) pour que les lecteurs
concurrents (ex. Bot2 RT lisant ``icao_mapping.json``) ne voient jamais un JSON tronqué.

Usage ::

    python bots/bot1/Long\\ Term/scripts/sync_rt_mapping_from_active_markets.py
    python .../sync_rt_mapping_from_active_markets.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# .../POLYMARKET/bots/bot1/Long Term/scripts/ -> .../POLYMARKET
_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parents[3]  # .../POLYMARKET (scripts → Long Term → bot1 → bots → root)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import market_manager as mm  # noqa: E402
from monitoring.error_guard import run_guarded  # noqa: E402


def _yes_if_above_for_temp(variable: str, operator: str) -> bool:
    v = (variable or "").lower()
    o = (operator or "gte").lower()
    if v == "temp_min":
        return o in ("gte", "gt", "eq")
    if o in ("lte", "lt"):
        return False
    return True


def _threshold_unit_for_json(unit: str) -> str:
    u = (unit or "f").lower()
    if u in ("c", "celsius", "°c"):
        return "C"
    return "F"


def _active_to_rt_entry(m: mm.ActiveMarket) -> dict[str, Any] | None:
    var = (m.variable or "").lower()
    if var not in ("temp_max", "temp_min", "temp_avg"):
        return None
    code = (m.station_code or "").strip().upper()
    if mm._looks_like_wmo(code):
        return None
    if not mm._looks_like_icao_or_faa(code):
        return None
    tz = (m.locality_tz or "").strip()
    if not tz:
        return None
    td = (m.target_date or "").strip()
    tds = (m.target_date_start or "").strip()
    tde = (m.target_date_end or "").strip()
    if not td and not (tds and tde):
        return None
    rec: dict[str, Any] = {
        "icao": code,
        "threshold": float(m.threshold),
        "threshold_unit": _threshold_unit_for_json(m.unit),
        "yes_if_above": _yes_if_above_for_temp(m.variable, m.operator),
        "timezone": tz,
        "deadline_local_hhmm": "23:59",
        "dead_min_gap": 15.0,
        "weather_variable": var,
    }
    if tds and tde:
        rec["target_date_start"] = tds
        rec["target_date_end"] = tde
    if td:
        rec["target_date"] = td
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="active_markets.json → icao_mapping.json (bot2 RT)")
    ap.add_argument(
        "--active",
        type=Path,
        default=mm.default_markets_json_path(),
        help="Chemin active_markets.json",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=_ROOT / "data" / "icao_mapping.json",
        help="Sortie icao_mapping.json",
    )
    ap.add_argument("--dry-run", action="store_true", help="Affiche le diff sans écrire")
    args = ap.parse_args()

    markets = mm.load_active_markets(args.active)
    out_path: Path = args.output
    existing: dict[str, Any] = {}
    if out_path.is_file():
        raw = json.loads(out_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            existing = raw

    merged = dict(existing)
    updated = 0
    skipped = 0
    for m in markets:
        entry = _active_to_rt_entry(m)
        if entry is None:
            skipped += 1
            continue
        mid = (m.market_id or "").strip()
        if not mid:
            skipped += 1
            continue
        merged[mid] = entry
        updated += 1

    print(
        f"Marchés actifs lus: {len(markets)} | entrées RT écrites: {updated} | ignorés: {skipped} | "
        f"clés totales mapping: {len(merged)}",
        flush=True,
    )
    if args.dry_run:
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Même répertoire que la cible : rename atomique (POSIX) ; lecteurs voient l’ancienne ou la nouvelle version entière.
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    payload = json.dumps(merged, ensure_ascii=False, indent=4)
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, out_path)
    print(f"Écrit: {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_guarded("bot1.sync_rt_mapping_from_active_markets", main))
