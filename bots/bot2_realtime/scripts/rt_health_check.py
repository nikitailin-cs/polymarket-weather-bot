#!/usr/bin/env python3
"""
Vérifie la fraîcheur de ``realtime_signals_meta.json`` et résume la santé des signaux.

Sortie non-zéro si le manifeste est absent, trop vieux (``REALTIME_SIGNALS_MAX_AGE_SEC``),
ou si ``--fail-on-degraded`` et ``signals_health.health_status != ok``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.bot2_data_paths import REALTIME_SIGNALS_MAX_AGE_SEC, get_rt_paths  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Santé bot2 RT — meta + signaux")
    ap.add_argument(
        "--max-age-sec",
        type=int,
        default=None,
        help=f"TTL max (défaut env REALTIME_SIGNALS_MAX_AGE_SEC ou {REALTIME_SIGNALS_MAX_AGE_SEC})",
    )
    ap.add_argument(
        "--fail-on-degraded",
        action="store_true",
        help="Code retour 2 si health_status du meta est degraded (ou error)",
    )
    args = ap.parse_args()

    paths = get_rt_paths()
    meta_path = paths["meta"]
    max_age = int(args.max_age_sec) if args.max_age_sec is not None else REALTIME_SIGNALS_MAX_AGE_SEC

    if not meta_path.is_file():
        print(f"ERROR: meta absent: {meta_path}", file=sys.stderr)
        return 1

    raw = json.loads(meta_path.read_text(encoding="utf-8"))
    ts = int(raw.get("generated_at_unix") or raw.get("timestamp") or 0)
    age = int(time.time()) - ts
    health = raw.get("signals_health") or {}
    hstat = str(health.get("health_status", "") or "unknown")

    print(
        f"meta={meta_path} age_sec={age} max_age_sec={max_age} health_status={hstat} "
        f"markets_processed={raw.get('markets_processed', '?')}",
        flush=True,
    )

    if ts <= 0:
        print("ERROR: timestamp meta invalide", file=sys.stderr)
        return 1
    if age > max_age:
        print(f"ERROR: meta trop vieux ({age}s > {max_age}s)", file=sys.stderr)
        return 1

    if args.fail_on_degraded and hstat != "ok":
        print(f"ERROR: health degraded: {hstat}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
