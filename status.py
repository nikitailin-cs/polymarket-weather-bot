"""Affiche l'état du bot sans le lancer."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from monitoring.pnl_tracker import print_open_positions, print_pnl_summary
from signals.sources.model_calibrator import print_bias_report


def _load_bot1_status_module():
    path = ROOT / "bots" / "bot1" / "execution" / "status.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_bot1_exec_status", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    print(
        "\n"
        "  Sources : (1) JSON pnl_tracker — souvent [DRY] si DRY_RUN=1 (≠ portefeuille Polymarket) ;\n"
        "            (2) portfolio.sqlite (PORTFOLIO_SQLITE_PATH / défaut data/) — finance_engine ;\n"
        "            (3) état CLOB live : .venv/bin/python bots/bot1/execution/status_live.py\n"
    )
    print("═" * 72)
    print("  (1) Positions / PnL — data/logs (pnl_tracker)")
    print("═" * 72)
    print_open_positions()
    print_pnl_summary()

    mod = _load_bot1_status_module()
    if mod is not None and hasattr(mod, "get_status"):
        print()
        print("═" * 72)
        print("  (2) Bot1 — derniers trades (voir PORTFOLIO_SQLITE_PATH / data/portfolio.sqlite)")
        print("═" * 72)
        mod.get_status(limit=15)
    else:
        print("\n  (2) bots/bot1/execution/status.py introuvable — ignoré.\n")

    print_bias_report()


if __name__ == "__main__":
    main()
