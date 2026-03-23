"""
Paramètres centralisés du bot.
Toutes les constantes passent par ici — ne jamais hardcoder dans les modules.
"""
import os
from pathlib import Path

# Charge .env si présent — DOIT être en premier
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optionnel en prod Docker (variables injectées directement)

# ── Polymarket ────────────────────────────────────────────────────────────────
CLOB_HOST   = "https://clob.polymarket.com"
CHAIN_ID    = 137           # Polygon mainnet
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# ── Scanner ───────────────────────────────────────────────────────────────────
MAX_MARKETS_RESULTS = int(os.getenv("MAX_RESULTS",     "500"))
SLEEP_BETWEEN_CALLS = float(os.getenv("SLEEP_SEC",     "0.05"))
MIN_VOLUME_USD      = float(os.getenv("MIN_VOLUME",    "500"))
MIN_DAYS_TO_EXPIRY  = int(os.getenv("MIN_DAYS_EXPIRY", "1"))

# ── Signaux météo ─────────────────────────────────────────────────────────────
OWM_API_KEY         = os.getenv("OWM_API_KEY", "")
WEATHER_REFRESH_SEC = int(os.getenv("WEATHER_REFRESH_SEC", "300"))

# ── Risk management ───────────────────────────────────────────────────────────
MIN_EDGE          = float(os.getenv("MIN_EDGE",          "0.05"))
MAX_BET_USD       = float(os.getenv("MAX_BET_USD",       "20.0"))
MAX_PORTFOLIO_USD = float(os.getenv("MAX_PORTFOLIO_USD", "150.0"))
KELLY_FRACTION    = float(os.getenv("KELLY_FRACTION",    "0.25"))
MAX_DRAWDOWN_PCT  = float(os.getenv("MAX_DRAWDOWN_PCT",  "0.30"))

# ── Fees CLOB Polymarket ──────────────────────────────────────────────────────
# Le CLOB Polymarket est actuellement à 0 fee (taker et maker).
# Garder FEE_BPS=0 et ajuster si Polymarket modifie sa grille.
# La déduction de coût dans edge_detector inclut déjà le spread estimé (2.5%).
FEE_BPS          = int(os.getenv("FEE_BPS",          "0"))
MAKER_REBATE_BPS = int(os.getenv("MAKER_REBATE_BPS", "0"))

# ── Stratégie de signal ───────────────────────────────────────────────────────
# YES side : historiquement 0% winrate sur les marchés météo (sur-estimation du marché).
# Mettre à "1" pour réactiver si les conditions changent.
ENABLE_YES_SIDE = os.getenv("ENABLE_YES_SIDE", "0") == "1"

# ── Monitoring ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")
LOG_DIR            = os.path.join(os.path.dirname(__file__), "..", "data", "logs")
LOG_LEVEL          = os.getenv("LOG_LEVEL", "INFO")

# ── Mode ──────────────────────────────────────────────────────────────────────
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

# ── Contractuel / Oracle (v2) ─────────────────────────────────────────────────
# Seuil minimum d'alignement modèle ↔ source oracle pour trader un marché.
# Sous ce seuil, le parsing est jugé insuffisant et le marché est écarté.
MIN_ORACLE_ALIGNMENT     = float(os.getenv("MIN_ORACLE_ALIGNMENT",     "0.70"))

# Polling oracle pendant CLOSED_PENDING (secondes entre deux vérifications).
ORACLE_POLL_INTERVAL_SEC = int(os.getenv("ORACLE_POLL_INTERVAL_SEC",   "1800"))

# Délai max avant alerte Telegram si l'oracle ne répond pas (heures).
MAX_ORACLE_WAIT_HOURS    = int(os.getenv("MAX_ORACLE_WAIT_HOURS",       "48"))

# ── Chemins fichiers de persistance ───────────────────────────────────────────
# Utilise des chemins absolus depuis la racine du projet pour éviter
# tout conflit selon le CWD au lancement.
_ROOT = Path(__file__).parent.parent

CONTRACT_SPECS_FILE = os.getenv(
    "CONTRACT_SPECS_FILE",
    str(_ROOT / "data" / "contract_specs.json"),
)
POSITIONS_FILE = os.getenv(
    "POSITIONS_FILE",
    str(_ROOT / "data" / "logs" / "positions.json"),
)
TRADES_FILE = os.getenv(
    "TRADES_FILE",
    str(_ROOT / "data" / "logs" / "trades.json"),
)
PNL_FILE = os.getenv(
    "PNL_FILE",
    str(_ROOT / "data" / "logs" / "pnl.json"),
)

# ── API LLM (optionnel — parsing ContractSpec) ────────────────────────────────
# Sans clé : le bot fonctionne uniquement avec le parsing regex (couvre ~90% des cas).
# Avec clé : le fallback LLM prend en charge les questions ambiguës.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")