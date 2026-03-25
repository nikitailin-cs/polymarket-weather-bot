#!/usr/bin/env python3
"""
Diff d'ensembles sur ``market_id`` entre deux exécutions de scan Gamma (Bot1).

- ``compute`` : ``Nouveaux = Current - Previous`` ; bootstrap si fichier d'état absent.
- ``persist`` : écrit l'ensemble courant (après scan réussi), format JSON stable.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_market_ids(active_json: Path) -> set[str]:
    if not active_json.is_file():
        raise FileNotFoundError(f"fichier actif introuvable: {active_json}")
    data = json.loads(active_json.read_text(encoding="utf-8-sig"))
    markets = data.get("markets") or []
    out: set[str] = set()
    for m in markets:
        mid = m.get("market_id")
        if mid is not None and str(mid).strip():
            out.add(str(mid).strip())
    return out


def _load_previous_ids(state_path: Path) -> tuple[set[str], bool]:
    """Retourne (previous_ids, bootstrap)."""
    if not state_path.is_file():
        return set(), True
    try:
        data = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return set(), True
    raw = data.get("market_ids")
    if not isinstance(raw, list):
        return set(), True
    prev = {str(x).strip() for x in raw if x is not None and str(x).strip()}
    return prev, False


def cmd_compute(args: argparse.Namespace) -> int:
    active = Path(args.active).resolve()
    state = Path(args.state).resolve()
    try:
        current = _load_market_ids(active)
    except FileNotFoundError as e:
        print(f"[update_bot] bot1_active_markets_state: {e}", file=sys.stderr)
        return 1

    previous, bootstrap = _load_previous_ids(state)
    if bootstrap:
        new: set[str] = set()
        added_count = 0
    else:
        new = current - previous
        added_count = len(new)

    sample = ",".join(sorted(new)[:12])
    if len(sample) > 280:
        sample = sample[:280] + "..."
    print(
        f"[update_bot] bot1_active_markets: added_count={added_count} "
        f"bootstrap={int(bootstrap)} current_total={len(current)} sample={sample!r}",
        file=sys.stderr,
    )
    print(f"export BOT1_ADDED_MARKETS_COUNT={added_count}")
    print(f"export BOT1_BOOTSTRAP={1 if bootstrap else 0}")
    return 0


def cmd_persist(args: argparse.Namespace) -> int:
    active = Path(args.active).resolve()
    state = Path(args.state).resolve()
    try:
        ids = _load_market_ids(active)
    except FileNotFoundError as e:
        print(f"[update_bot] bot1_active_markets_state persist: {e}", file=sys.stderr)
        return 1
    state.parent.mkdir(parents=True, exist_ok=True)
    payload = {"market_ids": sorted(ids)}
    state.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="État market_id Bot1 (diff Gamma).")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("compute", help="Calcule Current - Previous et exporte des variables bash.")
    pc.add_argument("--active", type=Path, required=True, help="Chemin vers data/active_markets.json")
    pc.add_argument("--state", type=Path, required=True, help="Chemin vers bot1_active_markets_ids.json")
    pc.set_defaults(func=cmd_compute)

    pp = sub.add_parser("persist", help="Écrit l'ensemble courant dans le fichier d'état.")
    pp.add_argument("--active", type=Path, required=True)
    pp.add_argument("--state", type=Path, required=True)
    pp.set_defaults(func=cmd_persist)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
