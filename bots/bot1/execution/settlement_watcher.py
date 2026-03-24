#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# .../POLYMARKET/bots/bot1/execution/settlement_watcher.py -> .../POLYMARKET
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

LOG = logging.getLogger("settlement_watcher")
GAMMA_URL = "https://gamma-api.polymarket.com/markets"
HTTP_TIMEOUT_SEC = 20


def _portfolio_db_path() -> Path:
    p = _ROOT / "data" / "portfolio.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _http_get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "settlement-watcher/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        return json.loads(resp.read())


def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_portfolio_db_path()))
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
    return conn


def _load_open_markets(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT market_id
        FROM executed_trades
        WHERE status = 'OPEN'
          AND market_id IS NOT NULL
          AND market_id != ''
        """
    ).fetchall()
    return [str(r[0]) for r in rows]


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
    rowcount = conn.execute(
        """
        UPDATE executed_trades
        SET status = 'RESOLVED'
        WHERE market_id = ?
          AND status = 'OPEN'
        """,
        (market_id,),
    ).rowcount
    conn.commit()
    return int(rowcount or 0)


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
        description="Watcher settlement: OPEN -> RESOLVED quand le marché est clos/résolu."
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
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    if args.loop:
        return run_loop(interval_sec=max(1, args.interval_sec), startup_delay_sec=max(0, args.startup_delay_sec))
    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
