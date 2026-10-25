"""
Paper trading: simulated fills and settlement, no wallet, no CLOB orders.

- ``record_paper_fill``: the finance engine's DRY-RUN branch records the order it
  would have sent (best ask, sized shares) as an OPEN position in
  ``portfolio_paper.sqlite`` (same ``positions`` schema as the live book, so the
  dashboard and the history work unchanged). One position per (market, side).
- ``settle_paper_positions``: OPEN positions whose market is closed on Gamma with a
  final outcome become SETTLED with ``pnl = payout - cost`` (payout = shares if the
  bought side won, else 0). Markets closed without a final price stay OPEN.

Environment:

- ``PAPER_TRADING``                 ``1`` (default when DRY_RUN) records paper fills; ``0`` logs only
- ``BOT2_PORTFOLIO_PAPER_SQLITE_PATH`` default ``data/bots/bot_2_realtime/portfolio_paper.sqlite``
- ``GAMMA_API_URL``                 default ``https://gamma-api.polymarket.com``
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from core.bot2_portfolio_schema import ensure_bot2_positions_table, insert_open_position

LOG = logging.getLogger("paper_broker")
_ROOT = Path(__file__).resolve().parent.parent


def paper_enabled() -> bool:
    return (os.getenv("PAPER_TRADING") or "1").strip().lower() not in ("0", "false", "no", "off")


def paper_portfolio_path() -> Path:
    raw = (os.getenv("BOT2_PORTFOLIO_PAPER_SQLITE_PATH") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (_ROOT / p).resolve()
    return (_ROOT / "data" / "bots" / "bot_2_realtime" / "portfolio_paper.sqlite").resolve()


def ensure_strategy_column(conn: sqlite3.Connection) -> None:
    cols = {str(c[1]) for c in conn.execute("PRAGMA table_info(positions)").fetchall()}
    if "strategy" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN strategy TEXT")
        conn.commit()


def has_position(conn: sqlite3.Connection, market_id: str, side: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM positions WHERE market_id = ? AND side = ? LIMIT 1", (market_id, side.upper())
    ).fetchone()
    return row is not None


def record_paper_fill(
    db_path: Path,
    *,
    market_id: str,
    side: str,
    price: float,
    shares: float,
    now: Optional[datetime] = None,
    strategy: str = "",
) -> bool:
    """Insert an OPEN paper position; returns False when one already exists for (market, side)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ts = (now or datetime.now(timezone.utc)).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    with closing(sqlite3.connect(str(db_path))) as conn:
        ensure_bot2_positions_table(conn)
        ensure_strategy_column(conn)
        if has_position(conn, market_id, side):
            LOG.info("paper: position already exists market=%s side=%s, skipped", market_id[:16], side)
            return False
        insert_open_position(conn, market_id=market_id, side=side.upper(), price=float(price), size=float(shares), timestamp_iso_utc=ts)
        if strategy:
            conn.execute("UPDATE positions SET strategy = ? WHERE rowid = (SELECT max(rowid) FROM positions)", (strategy,))
        conn.commit()
    LOG.info("paper: OPEN market=%s side=%s price=%.4f shares=%.4f cost=%.2f strategy=%s", market_id[:16], side, price, shares, price * shares, strategy or "-")
    return True


# -- settlement ------------------------------------------------------------------------

def fetch_gamma_markets(condition_ids: list[str], base_url: Optional[str] = None) -> dict[str, dict[str, Any]]:
    import httpx

    base = (base_url or os.getenv("GAMMA_API_URL") or "https://gamma-api.polymarket.com").rstrip("/")
    out: dict[str, dict[str, Any]] = {}
    ids = [c for c in condition_ids if c]
    for i in range(0, len(ids), 20):
        chunk = ids[i : i + 20]
        r = httpx.get(f"{base}/markets", params=[("condition_ids", c) for c in chunk], timeout=15.0)
        r.raise_for_status()
        for m in r.json() or []:
            cid = str(m.get("conditionId") or "")
            if cid:
                out[cid] = m
    return out


def final_outcome(market: dict[str, Any]) -> Optional[str]:
    """``"YES"`` / ``"NO"`` once Gamma shows a closed market with a final price, else None."""
    if not market.get("closed"):
        return None
    prices = market.get("outcomePrices")
    try:
        plist = [float(x) for x in (json.loads(prices) if isinstance(prices, str) else prices or [])]
    except (ValueError, TypeError):
        return None
    if len(plist) < 2:
        return None
    outcomes = market.get("outcomes")
    try:
        names = [str(x).upper() for x in (json.loads(outcomes) if isinstance(outcomes, str) else outcomes or [])]
    except (ValueError, TypeError):
        names = []
    if names[:2] != ["YES", "NO"]:
        names = ["YES", "NO"]
    winners = [names[i] for i, p in enumerate(plist[:2]) if p >= 0.999]
    losers = [p for p in plist[:2] if p <= 0.001]
    if len(winners) == 1 and len(losers) == 1:
        return winners[0]
    return None


def settle_paper_positions(
    db_path: Path,
    *,
    fetch: Callable[[list[str]], dict[str, dict[str, Any]]] = fetch_gamma_markets,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Settle OPEN paper positions against Gamma; returns counts and realized PnL of this run."""
    if not db_path.is_file():
        return {"open": 0, "settled": 0, "pnl": 0.0}
    ts = (now or datetime.now(timezone.utc)).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    with closing(sqlite3.connect(str(db_path))) as conn:
        ensure_bot2_positions_table(conn)
        rows = conn.execute(
            "SELECT rowid, market_id, side, price, size FROM positions WHERE status = 'OPEN'"
        ).fetchall()
        if not rows:
            return {"open": 0, "settled": 0, "pnl": 0.0}
        try:
            markets = fetch(sorted({r[1] for r in rows}))
        except Exception as e:  # noqa: BLE001
            LOG.warning("paper settlement: Gamma unavailable: %s", e)
            return {"open": len(rows), "settled": 0, "pnl": 0.0, "error": str(e)}
        settled = 0
        total = 0.0
        for rowid, market_id, side, price, size in rows:
            m = markets.get(market_id)
            if not m:
                continue
            winner = final_outcome(m)
            if winner is None:
                continue
            payout = float(size) if winner == str(side).upper() else 0.0
            pnl = round(payout - float(price) * float(size), 4)
            conn.execute(
                "UPDATE positions SET status = 'SETTLED', pnl = ?, settled_at = ? WHERE rowid = ?",
                (pnl, ts, rowid),
            )
            settled += 1
            total += pnl
            LOG.info("paper: SETTLED market=%s side=%s winner=%s pnl=%+.2f", market_id[:16], side, winner, pnl)
        conn.commit()
    return {"open": len(rows) - settled, "settled": settled, "pnl": round(total, 4)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    res = settle_paper_positions(paper_portfolio_path())
    LOG.info("paper settlement: %s", res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
