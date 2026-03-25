#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Point d'entrée **ops** pour le producteur de signaux NOAA bot1.

Délègue à ``run_nomads_live_test.py`` (nom historique conservé pour compatibilité
scripts et chemins). Mêmes arguments CLI.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from monitoring.error_guard import run_guarded

_SIGNAL_PRODUCER = Path(__file__).resolve().parent / "run_nomads_live_test.py"


def main() -> int:
    spec = importlib.util.spec_from_file_location("_bot1_signal_producer_impl", _SIGNAL_PRODUCER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Impossible de charger {_SIGNAL_PRODUCER}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "main", None)
    if fn is None:
        raise RuntimeError("run_nomads_live_test.main() introuvable")
    return int(fn())


if __name__ == "__main__":
    raise SystemExit(run_guarded("bot1.produce_trading_signals", main))
