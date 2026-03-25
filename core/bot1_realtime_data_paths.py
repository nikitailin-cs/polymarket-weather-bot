"""
Chemins du bot 1 temps réel — alias de ``core.bot_rt_data_paths``.

L’implémentation et les défauts (``data/bots/realtime/``, ``data/icao_mapping.json``)
sont dans ``bot_rt_data_paths`` ; les variables ``BOT1_REALTIME_*`` restent supportées.
"""
from __future__ import annotations

from core.bot_rt_data_paths import (
    icao_mapping_path,
    last_realtime_run_path,
    portfolio_rt_sqlite_path,
    realtime_signals_meta_path,
    realtime_signals_path,
    realtime_watchdog_state_path,
)

__all__ = [
    "icao_mapping_path",
    "last_realtime_run_path",
    "portfolio_rt_sqlite_path",
    "realtime_signals_meta_path",
    "realtime_signals_path",
    "realtime_watchdog_state_path",
]
