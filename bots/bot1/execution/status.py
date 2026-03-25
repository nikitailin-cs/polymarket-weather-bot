#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# .../POLYMARKET/bots/bot1/execution/status.py -> .../POLYMARKET
_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.bot1_data_paths import portfolio_sqlite_path  # noqa: E402
from monitoring.error_guard import run_guarded  # noqa: E402


def _db_path() -> Path:
    return portfolio_sqlite_path()


def _fetch_last_trades(limit: int = 10) -> list[tuple]:
    db = _db_path()
    if not db.is_file():
        return []
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            """
            SELECT
                traded_at_utc,
                market_id,
                recommended_side,
                effective_p_win,
                best_ask,
                ev
            FROM executed_trades
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return rows
    finally:
        conn.close()


def _fmt_market_id(market_id: str) -> str:
    m = str(market_id)
    if len(m) <= 14:
        return m
    return f"{m[:8]}...{m[-6:]}"


def _print_plain(rows: list[tuple]) -> None:
    if not rows:
        print(f"Aucune position/trade trouvé dans {_db_path()}.")
        return
    print("POLYMETEO - LAST TRADES")
    print("-" * 90)
    print(f"{'UTC':20} {'MARKET':18} {'SIDE':4} {'PWIN':>6} {'ASK':>8} {'EV':>8}")
    print("-" * 90)
    for traded_at_utc, market_id, side, p_win, ask, ev in rows:
        print(
            f"{str(traded_at_utc):20} "
            f"{_fmt_market_id(market_id):18} "
            f"{str(side):4} "
            f"{float(p_win):6.3f} "
            f"{float(ask):8.3f} "
            f"{float(ev):8.3f}"
        )


def _print_rich(rows: list[tuple]) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    if not rows:
        console.print(f"[yellow]Aucune position/trade trouvé dans {_db_path()}.[/yellow]")
        return

    table = Table(title="POLYMETEO - LIVE STATUS (Last Trades)")
    table.add_column("Date (UTC)", style="cyan")
    table.add_column("Marché", style="magenta")
    table.add_column("Side", style="bold")
    table.add_column("P(Win)", justify="right")
    table.add_column("Ask", justify="right")
    table.add_column("EV", justify="right", style="green")

    for traded_at_utc, market_id, side, p_win, ask, ev in rows:
        side_s = str(side).upper()
        side_style = "green" if side_s == "YES" else "red"
        table.add_row(
            str(traded_at_utc),
            _fmt_market_id(market_id),
            f"[{side_style}]{side_s}[/{side_style}]",
            f"{float(p_win):.3f}",
            f"{float(ask):.3f}",
            f"{float(ev):.3f}",
        )

    console.print(table)


def get_status(limit: int = 10) -> None:
    rows = _fetch_last_trades(limit=limit)
    try:
        _print_rich(rows)
    except Exception:
        _print_plain(rows)


if __name__ == "__main__":
    raise SystemExit(run_guarded("bot1.status", lambda: (get_status(limit=10) or 0)))
