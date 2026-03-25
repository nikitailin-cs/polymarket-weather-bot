"""
Chemins de persistance du pipeline bot1 (NOAA, finance_engine, settlement).

Variables d'environnement (chemins absolus ou relatifs à la racine du repo) :

- ``TRADING_SIGNALS_FILE`` — défaut ``data/trading_signals.json``
- ``TRADING_SIGNALS_META_FILE`` — défaut ``data/trading_signals_meta.json``
- ``PORTFOLIO_SQLITE_PATH`` — défaut ``data/portfolio.sqlite``
- ``NOAA_WATCHDOG_STATE_PATH`` — défaut ``data/logs/noaa_watchdog_state.json``
- ``LAST_NOAA_RUN_FILE`` — défaut ``data/last_noaa_run.json``

``main_compartment.py`` peut définir ces variables pour un compartiment ``data/bots/bot_1/``.
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _resolve_path(env_var: str, *default_segments: str) -> Path:
    raw = (os.getenv(env_var) or "").strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = _REPO / p
        return p.resolve()
    return _REPO.joinpath(*default_segments).resolve()


def trading_signals_path() -> Path:
    return _resolve_path("TRADING_SIGNALS_FILE", "data", "trading_signals.json")


def trading_signals_meta_path() -> Path:
    return _resolve_path("TRADING_SIGNALS_META_FILE", "data", "trading_signals_meta.json")


def portfolio_sqlite_path() -> Path:
    return _resolve_path("PORTFOLIO_SQLITE_PATH", "data", "portfolio.sqlite")


def noaa_watchdog_state_path() -> Path:
    return _resolve_path("NOAA_WATCHDOG_STATE_PATH", "data", "logs", "noaa_watchdog_state.json")


def last_noaa_run_path() -> Path:
    return _resolve_path("LAST_NOAA_RUN_FILE", "data", "last_noaa_run.json")
