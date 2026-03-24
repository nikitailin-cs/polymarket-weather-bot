import logging
import os
import sys
from datetime import datetime

try:
    from config.settings import LOG_DIR, LOG_LEVEL
except Exception:
    LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "logs")
    LOG_LEVEL = "INFO"

def _setup_logger() -> logging.Logger:
    log_dir = os.getenv("POLYBOT_LOG_DIR", LOG_DIR)
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"bot_{datetime.utcnow().strftime('%Y%m%d')}.log")
    fmt = logging.Formatter("[%(asctime)s] %(levelname)-8s — %(message)s", datefmt="%H:%M:%S")
    logger = logging.getLogger("polybot")
    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    if not logger.handlers:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger

log = _setup_logger()