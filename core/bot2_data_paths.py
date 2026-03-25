"""
Isolation des chemins **bot 2 temps réel** : JSON et SQLite sous ``data/bots/bot_2_realtime/``,
logs sous ``logs/bot2_realtime/``. Le mapping OACI peut rester le référentiel partagé
``data/icao_mapping.json``.

Utiliser ``get_rt_paths()`` pour injection rapide (tests, scripts).
"""
from __future__ import annotations

import os
from pathlib import Path

# Racine du projet (remonte d'un niveau par rapport à 'core/')
BASE_DIR = Path(__file__).resolve().parent.parent

# Dossier d'isolation pour le Temps Réel
BOT2_DATA_DIR = BASE_DIR / "data" / "bots" / "bot_2_realtime"
BOT2_DATA_DIR.mkdir(parents=True, exist_ok=True)

# 1. Le Mapping statique (La Carte)
BOT2_ICAO_MAPPING_FILE = BASE_DIR / "data" / "icao_mapping.json"

# 2. Les outputs du producteur de signaux
REALTIME_SIGNALS_FILE = BOT2_DATA_DIR / "realtime_signals.json"
REALTIME_SIGNALS_META_FILE = BOT2_DATA_DIR / "realtime_signals_meta.json"

# 3. La base de données financière isolée
BOT2_PORTFOLIO_SQLITE_PATH = BOT2_DATA_DIR / "portfolio_rt.sqlite"

# 4. Logs spécifiques
BOT2_LOGS_DIR = BASE_DIR / "logs" / "bot2_realtime"
BOT2_LOGS_DIR.mkdir(parents=True, exist_ok=True)

# 5. Configuration de la fraîcheur (anti-stale) - 180 secondes max
REALTIME_SIGNALS_MAX_AGE_SEC = int(os.getenv("REALTIME_SIGNALS_MAX_AGE_SEC", "180"))


def get_rt_paths() -> dict[str, Path]:
    """Renvoie un dictionnaire des chemins critiques pour injection rapide."""
    return {
        "mapping": BOT2_ICAO_MAPPING_FILE,
        "signals": REALTIME_SIGNALS_FILE,
        "meta": REALTIME_SIGNALS_META_FILE,
        "db": BOT2_PORTFOLIO_SQLITE_PATH,
    }
