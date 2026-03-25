#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# .../POLYMARKET/bots/bot1/execution/settlement_watcher.py -> .../POLYMARKET
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.bot1_data_paths import portfolio_sqlite_path  # noqa: E402
from core.bot1_http import bot1_http_get_json  # noqa: E402
from monitoring.error_guard import run_guarded  # noqa: E402

LOG = logging.getLogger("settlement_watcher")
GAMMA_URL = "https://gamma-api.polymarket.com/markets"
HTTP_TIMEOUT_SEC = 20

# Surcharge CLI (ex. bot 1 temps réel : ``portfolio_rt.sqlite``).
_portfolio_sqlite_override: Optional[Path] = None


def set_portfolio_sqlite_path(path: Optional[Path]) -> None:
    """Fixe la base SQLite (API programmatique ; sinon ``--portfolio-sqlite`` / défaut bot1)."""
    global _portfolio_sqlite_override
    if path is None:
        _portfolio_sqlite_override = None
    else:
        _portfolio_sqlite_override = path.expanduser().resolve()


def _portfolio_db_path() -> Path:
    if _portfolio_sqlite_override is not None:
        p = _portfolio_sqlite_override
    else:
        p = portfolio_sqlite_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _http_get_json(url: str) -> Any:
    return bot1_http_get_json(url, timeout=float(HTTP_TIMEOUT_SEC))


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _is_bot2_rt_portfolio_path(path: Path) -> bool:
    """Base bot 2 RT (``data/bots/bot_2_realtime/…``) : table ``positions``, pas ``executed_trades`` seule."""
    return "bot_2_realtime" in str(path).replace("\\", "/")


def _ensure_executed_trades_schema(conn: sqlite3.Connection) -> None:
    """Schéma batch / bot1 RT (``executed_trades``)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS executed_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            traded_at_utc TEXT NOT NULL,
            traded_date_utc TEXT NOT NULL,
            forecast_cycle_utc TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'OPEN',
            pnl REAL,
            settled_at TEXT,
            market_id TEXT NOT NULL,
            station TEXT,
            recommended_side TEXT NOT NULL,
            p_win REAL NOT NULL,
            effective_p_win REAL NOT NULL,
            delta REAL NOT NULL,
            best_ask REAL NOT NULL,
            ev REAL NOT NULL,
            barbell_condition TEXT NOT NULL,
            token_id TEXT NOT NULL,
            order_id TEXT,
            raw_response_json TEXT
        )
        """
    )
    cols = conn.execute("PRAGMA table_info(executed_trades)").fetchall()
    col_names = {str(c[1]) for c in cols}
    if "status" not in col_names:
        conn.execute("ALTER TABLE executed_trades ADD COLUMN status TEXT NOT NULL DEFAULT 'OPEN'")
    if "pnl" not in col_names:
        conn.execute("ALTER TABLE executed_trades ADD COLUMN pnl REAL")
    if "settled_at" not in col_names:
        conn.execute("ALTER TABLE executed_trades ADD COLUMN settled_at TEXT")
    conn.execute(
        """
        UPDATE executed_trades
        SET status = 'OPEN'
        WHERE status IS NULL OR TRIM(status) = ''
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_executed_trades_status_market
        ON executed_trades (status, market_id)
        """
    )
    conn.commit()


def _open_db() -> sqlite3.Connection:
    """
    Connexion à la base pointée par ``--portfolio-sqlite`` / défaut.

    - Bot 1 (batch / RT classique) : ``executed_trades``.
    - Bot 2 temps réel : ``positions`` (``core.bot2_portfolio_schema``), sans créer
      ``executed_trades`` sur une base vide dédiée bot2.
    """
    path = _portfolio_db_path()
    conn = sqlite3.connect(str(path))
    tables = _table_names(conn)
    bot2 = _is_bot2_rt_portfolio_path(path)

    if "executed_trades" in tables or not bot2:
        _ensure_executed_trades_schema(conn)

    if bot2 or "positions" in tables:
        from core.bot2_portfolio_schema import ensure_bot2_positions_table  # noqa: WPS433

        ensure_bot2_positions_table(conn)

    return conn


def _load_open_markets(conn: sqlite3.Connection) -> list[str]:
    """Union des ``market_id`` OPEN dans ``executed_trades`` et/ou ``positions`` (bot2)."""
    ids: set[str] = set()
    if "executed_trades" in _table_names(conn):
        rows = conn.execute(
            """
            SELECT DISTINCT market_id
            FROM executed_trades
            WHERE status = 'OPEN'
              AND market_id IS NOT NULL
              AND market_id != ''
            """
        ).fetchall()
        ids.update(str(r[0]) for r in rows if r and r[0])
    if "positions" in _table_names(conn):
        rows = conn.execute(
            """
            SELECT DISTINCT market_id
            FROM positions
            WHERE UPPER(TRIM(COALESCE(status, ''))) = 'OPEN'
              AND market_id IS NOT NULL
              AND market_id != ''
            """
        ).fetchall()
        ids.update(str(r[0]) for r in rows if r and r[0])
    return sorted(ids)


def _fetch_market_payload(market_id: str) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"condition_ids": market_id, "limit": 1})
    url = f"{GAMMA_URL}?{query}"
    data = _http_get_json(url)
    if not isinstance(data, list) or not data:
        return None
    raw = data[0]
    return raw if isinstance(raw, dict) else None


def _is_market_resolved_or_closed(market: dict[str, Any]) -> bool:
    if bool(market.get("resolved")):
        return True
    if bool(market.get("closed")):
        return True

    # Fallback utile: certains payloads exposent le résultat via outcomePrices.
    raw = market.get("outcomePrices") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    try:
        prices = [float(x) for x in raw]
    except Exception:
        prices = []
    if not prices:
        return False
    p_yes = prices[0]
    return p_yes >= 0.99 or p_yes <= 0.01


def _mark_market_resolved(conn: sqlite3.Connection, market_id: str) -> int:
    total = 0
    if "executed_trades" in _table_names(conn):
        total += int(
            conn.execute(
                """
                UPDATE executed_trades
                SET status = 'RESOLVED'
                WHERE market_id = ?
                  AND status = 'OPEN'
                """,
                (market_id,),
            ).rowcount
            or 0
        )
    if "positions" in _table_names(conn):
        total += int(
            conn.execute(
                """
                UPDATE positions
                SET status = 'RESOLVED'
                WHERE market_id = ?
                  AND UPPER(TRIM(COALESCE(status, ''))) = 'OPEN'
                """,
                (market_id,),
            ).rowcount
            or 0
        )
    conn.commit()
    return total


def run_once() -> int:
    conn = _open_db()
    checked = 0
    transitioned = 0
    try:
        market_ids = _load_open_markets(conn)
        if not market_ids:
            LOG.info("Aucun marché OPEN à surveiller.")
            return 0

        for market_id in market_ids:
            checked += 1
            try:
                payload = _fetch_market_payload(market_id)
            except Exception as exc:
                LOG.warning("Gamma error market_id=%s err=%s", market_id, exc)
                continue
            if not payload:
                LOG.info("Introuvable sur Gamma market_id=%s", market_id)
                continue
            if not _is_market_resolved_or_closed(payload):
                continue
            changed = _mark_market_resolved(conn, market_id)
            if changed > 0:
                transitioned += changed
                LOG.info("Transition OPEN->RESOLVED market_id=%s rows=%s", market_id, changed)
    finally:
        conn.close()
    LOG.info(
        "Watcher cycle finished at=%s checked_markets=%s transitioned_rows=%s",
        _utc_now_iso(),
        checked,
        transitioned,
    )
    return 0


def run_loop(interval_sec: int, startup_delay_sec: int) -> int:
    if startup_delay_sec > 0:
        LOG.info("Startup delay %ss avant premier scan.", startup_delay_sec)
        time.sleep(startup_delay_sec)
    while True:
        cycle_start = time.monotonic()
        run_once()
        elapsed = max(0, int(time.monotonic() - cycle_start))
        sleep_for = max(1, int(interval_sec) - elapsed)
        LOG.info("Sleep %ss avant prochain scan.", sleep_for)
        time.sleep(sleep_for)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watcher settlement: OPEN -> RESOLVED quand le marché est clos/résolu.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemples:\n"
            "  %(prog)s --portfolio-sqlite data/bots/realtime/portfolio_rt.sqlite\n"
            "  %(prog)s --loop --portfolio-sqlite data/bots/realtime/portfolio_rt.sqlite\n"
            "\n"
            "Sans --portfolio-sqlite : utilise PORTFOLIO_SQLITE_PATH / core.bot1_data_paths (batch bot1)."
        ),
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Mode daemon (sinon exécution unique).",
    )
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=3600,
        help="Intervalle entre scans en mode --loop (défaut: 3600).",
    )
    parser.add_argument(
        "--startup-delay-sec",
        type=int,
        default=900,
        help="Décalage initial pour désynchroniser du bot de trading (défaut: 900).",
    )
    parser.add_argument(
        "--portfolio-sqlite",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Base SQLite des lignes OPEN (tables executed_trades et/ou positions). "
            "Bot1 RT : ex. data/bots/realtime/portfolio_rt.sqlite. "
            "Bot2 RT METAR : ex. data/bots/bot_2_realtime/portfolio_rt.sqlite. "
            "Défaut sans ce flag : portfolio batch (PORTFOLIO_SQLITE_PATH)."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    set_portfolio_sqlite_path(args.portfolio_sqlite)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    if args.loop:
        return run_loop(interval_sec=max(1, args.interval_sec), startup_delay_sec=max(0, args.startup_delay_sec))
    return run_once()


if __name__ == "__main__":
    raise SystemExit(run_guarded("bot1.settlement_watcher", main))
