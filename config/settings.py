"""
Paramètres centralisés du bot.
Toutes les constantes passent par ici — ne jamais hardcoder dans les modules.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Polymarket ────────────────────────────────────────────────────────────────
CLOB_HOST      = "https://clob.polymarket.com"
CHAIN_ID       = 137          # Polygon mainnet
PRIVATE_KEY    = os.getenv("PRIVATE_KEY")

# ── Scanner ───────────────────────────────────────────────────────────────────
MAX_MARKETS_RESULTS   = int(os.getenv("MAX_RESULTS", "100"))
SLEEP_BETWEEN_CALLS   = float(os.getenv("SLEEP_SEC", "0.05"))
MIN_VOLUME_USD        = float(os.getenv("MIN_VOLUME", "500"))     # Ignorer < 500$
MIN_DAYS_TO_EXPIRY    = int(os.getenv("MIN_DAYS_EXPIRY", "1"))    # Ignorer marchés expirés/jour J

# ── Signaux météo ─────────────────────────────────────────────────────────────
OWM_API_KEY           = os.getenv("OWM_API_KEY", "")              # OpenWeatherMap
WEATHER_REFRESH_SEC   = int(os.getenv("WEATHER_REFRESH_SEC", "300"))  # 5 min

# ── Risk management ───────────────────────────────────────────────────────────
MIN_EDGE              = float(os.getenv("MIN_EDGE", "0.05"))      # Edge minimum pour trader (5%)
MAX_BET_USD           = float(os.getenv("MAX_BET_USD", "20.0"))
MAX_PORTFOLIO_USD     = float(os.getenv("MAX_PORTFOLIO_USD", "150.0"))
KELLY_FRACTION        = float(os.getenv("KELLY_FRACTION", "0.25")) # Kelly fractionnel (25%)
MAX_DRAWDOWN_PCT      = float(os.getenv("MAX_DRAWDOWN_PCT", "0.30"))  # Stop si -30%

# ── Monitoring ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "")
LOG_DIR               = os.path.join(os.path.dirname(__file__), "..", "data", "logs")
LOG_LEVEL             = os.getenv("LOG_LEVEL", "INFO")

# ── Mode ──────────────────────────────────────────────────────────────────────
DRY_RUN               = os.getenv("DRY_RUN", "1") == "1"   # True = pas d'ordres réels