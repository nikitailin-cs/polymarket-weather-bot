"""
Chemins du pipeline **temps réel** (bot 1 RT), isolés du batch long-terme.

Tous les fichiers JSON et ``portfolio_rt.sqlite`` vivent par défaut sous
``data/bots/realtime/`` — pas sous ``data/bots/bot_1/`` (NOAA / portfolio batch).

Variables d'environnement (chemins absolus ou relatifs à la racine du repo) ;
pour chaque fichier, la variable ``BOT1_REALTIME_*`` reste acceptée (legacy) :

- ``BOT_RT_SIGNALS_FILE`` / ``BOT1_REALTIME_SIGNALS_FILE`` —
  défaut ``data/bots/realtime/realtime_signals.json``
- ``BOT_RT_SIGNALS_META_FILE`` / ``BOT1_REALTIME_SIGNALS_META_FILE`` —
  défaut ``data/bots/realtime/realtime_signals_meta.json``
- ``BOT_RT_PORTFOLIO_SQLITE_PATH`` / ``BOT1_PORTFOLIO_RT_SQLITE_PATH`` —
  défaut ``data/bots/realtime/portfolio_rt.sqlite``
- ``BOT_RT_WATCHDOG_STATE_PATH`` / ``BOT1_REALTIME_WATCHDOG_STATE_PATH`` —
  défaut ``data/bots/realtime/logs/realtime_watchdog_state.json``
- ``BOT_RT_LAST_RUN_FILE`` / ``BOT1_LAST_REALTIME_RUN_FILE`` —
  défaut ``data/bots/realtime/last_realtime_run.json``
- ``BOT_RT_ICAO_MAPPING_FILE`` / ``BOT1_ICAO_MAPPING_FILE`` —
  défaut ``data/icao_mapping.json`` (référentiel marchés / OACI, hors dossier realtime)

``main_compartment.py --bot 1`` positionne les variables ``BOT1_REALTIME_*`` et
``BOT_RT_*`` vers le même dossier ``data/bots/realtime/``.
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _resolve_rt_path(legacy_var: str, rt_var: str, *default_segments: str) -> Path:
    raw = (os.getenv(legacy_var) or os.getenv(rt_var) or "").strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = _REPO / p
        return p.resolve()
    return _REPO.joinpath(*default_segments).resolve()


def realtime_signals_path() -> Path:
    return _resolve_rt_path(
        "BOT1_REALTIME_SIGNALS_FILE",
        "BOT_RT_SIGNALS_FILE",
        "data",
        "bots",
        "realtime",
        "realtime_signals.json",
    )


def realtime_signals_meta_path() -> Path:
    return _resolve_rt_path(
        "BOT1_REALTIME_SIGNALS_META_FILE",
        "BOT_RT_SIGNALS_META_FILE",
        "data",
        "bots",
        "realtime",
        "realtime_signals_meta.json",
    )


def portfolio_rt_sqlite_path() -> Path:
    return _resolve_rt_path(
        "BOT1_PORTFOLIO_RT_SQLITE_PATH",
        "BOT_RT_PORTFOLIO_SQLITE_PATH",
        "data",
        "bots",
        "realtime",
        "portfolio_rt.sqlite",
    )


def realtime_watchdog_state_path() -> Path:
    return _resolve_rt_path(
        "BOT1_REALTIME_WATCHDOG_STATE_PATH",
        "BOT_RT_WATCHDOG_STATE_PATH",
        "data",
        "bots",
        "realtime",
        "logs",
        "realtime_watchdog_state.json",
    )


def last_realtime_run_path() -> Path:
    return _resolve_rt_path(
        "BOT1_LAST_REALTIME_RUN_FILE",
        "BOT_RT_LAST_RUN_FILE",
        "data",
        "bots",
        "realtime",
        "last_realtime_run.json",
    )


def icao_mapping_path() -> Path:
    return _resolve_rt_path(
        "BOT1_ICAO_MAPPING_FILE",
        "BOT_RT_ICAO_MAPPING_FILE",
        "data",
        "icao_mapping.json",
    )
