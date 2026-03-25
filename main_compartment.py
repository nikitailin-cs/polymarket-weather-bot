"""
Lance un bot dans un compartiment de persistance isolé.

Dossiers : tout est sous data/bots/<id>/ (ex. bot_1, bot_2).

Définit aussi les variables NOAA / finance_engine / settlement (TRADING_SIGNALS_FILE,
PORTFOLIO_SQLITE_PATH, etc.) pour que le pipeline bot1 batch et main.py partagent le même
compartiment. Pour ``bot_1`` uniquement : chemins **temps réel** isolés sous
``data/bots/realtime/`` (BOT1_REALTIME_* / BOT_RT_* , ``portfolio_rt.sqlite``) ;
mapping OACI par défaut ``data/icao_mapping.json``.

Usage:
  python main_compartment.py --bot 1
  python main_compartment.py --bot 2
  python main_compartment.py --name bot_1
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def _set_isolated_paths(name: str) -> None:
    root = Path(__file__).resolve().parent
    compartment = root / "data" / "bots" / name

    logs_dir = compartment / "logs"
    ml_dir = compartment / "ml"
    processed_dir = compartment / "processed"

    # Pipeline NOAA / finance_engine / settlement (même compartiment que bot1 ops)
    os.environ.setdefault("TRADING_SIGNALS_FILE", str(compartment / "trading_signals.json"))
    os.environ.setdefault("TRADING_SIGNALS_META_FILE", str(compartment / "trading_signals_meta.json"))
    os.environ.setdefault("PORTFOLIO_SQLITE_PATH", str(compartment / "portfolio.sqlite"))
    os.environ.setdefault("NOAA_WATCHDOG_STATE_PATH", str(logs_dir / "noaa_watchdog_state.json"))
    os.environ.setdefault("LAST_NOAA_RUN_FILE", str(compartment / "last_noaa_run.json"))

    # Pipeline temps réel (METAR) — dossier dédié data/bots/realtime/ (hors batch NOAA)
    if name == "bot_1":
        rt_root = root / "data" / "bots" / "realtime"
        rt_logs = rt_root / "logs"
        sig = str(rt_root / "realtime_signals.json")
        meta = str(rt_root / "realtime_signals_meta.json")
        prt = str(rt_root / "portfolio_rt.sqlite")
        wd = str(rt_logs / "realtime_watchdog_state.json")
        last_rt = str(rt_root / "last_realtime_run.json")
        for key, val in (
            ("BOT1_REALTIME_SIGNALS_FILE", sig),
            ("BOT_RT_SIGNALS_FILE", sig),
            ("BOT1_REALTIME_SIGNALS_META_FILE", meta),
            ("BOT_RT_SIGNALS_META_FILE", meta),
            ("BOT1_PORTFOLIO_RT_SQLITE_PATH", prt),
            ("BOT_RT_PORTFOLIO_SQLITE_PATH", prt),
            ("BOT1_REALTIME_WATCHDOG_STATE_PATH", wd),
            ("BOT_RT_WATCHDOG_STATE_PATH", wd),
            ("BOT1_LAST_REALTIME_RUN_FILE", last_rt),
            ("BOT_RT_LAST_RUN_FILE", last_rt),
        ):
            os.environ.setdefault(key, val)
        root_icao = root / "data" / "icao_mapping.json"
        os.environ.setdefault("BOT1_ICAO_MAPPING_FILE", str(root_icao))
        os.environ.setdefault("BOT_RT_ICAO_MAPPING_FILE", str(root_icao))

    os.environ.setdefault("CONTRACT_SPECS_FILE", str(compartment / "contract_specs.json"))
    os.environ.setdefault("POSITIONS_FILE", str(logs_dir / "positions.json"))
    os.environ.setdefault("TRADES_FILE", str(logs_dir / "trades.json"))
    os.environ.setdefault("PNL_FILE", str(logs_dir / "pnl.json"))
    os.environ.setdefault("ONCHAIN_OPS_FILE", str(logs_dir / "onchain_ops.json"))

    os.environ.setdefault("POLYBOT_LOG_DIR", str(logs_dir))
    os.environ.setdefault("FEATURE_STORE_FILE", str(ml_dir / "feature_store.jsonl"))
    os.environ.setdefault("MODEL_BIAS_FILE", str(processed_dir / "model_bias.json"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    g = parser.add_mutually_exclusive_group()
    g.add_argument(
        "--bot",
        type=int,
        choices=(1, 2),
        help="Raccourci : 1 → data/bots/bot_1, 2 → data/bots/bot_2",
    )
    g.add_argument(
        "--name",
        default="",
        help="Identifiant sous data/bots/ (ex: bot_1, bot_2, test). Défaut si rien : bot_2",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.bot is not None:
        compartment_name = f"bot_{args.bot}"
    else:
        raw = (args.name or "").strip().lower().replace(" ", "_")
        if raw in ("1", "2"):
            compartment_name = f"bot_{raw}"
        elif raw == "bot1":
            compartment_name = "bot_1"
        elif raw == "bot2":
            compartment_name = "bot_2"
        else:
            compartment_name = raw or "bot_2"
    if not compartment_name:
        raise SystemExit("Le nom de compartiment ne peut pas être vide.")

    _set_isolated_paths(compartment_name)

    from main import run
    run()


if __name__ == "__main__":
    main()
