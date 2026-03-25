#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# .../POLYMARKET/bots/bot1/execution/exit_engine.py -> .../POLYMARKET
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.bot1_data_paths import portfolio_sqlite_path  # noqa: E402
from core.bot1_http import bot1_http_get_json  # noqa: E402
from execution.ctf_manager import (  # noqa: E402
    RedeemResult,
    get_pol_balance,
    get_usdc_balance,
    redeem_position_detailed,
)
from monitoring.telegram_alert import (  # noqa: E402
    alert_bot1_position_close,
    alert_error,
    send_alert,
)
from monitoring.error_guard import run_guarded  # noqa: E402

LOG = logging.getLogger("exit_engine")
GAMMA_URL = "https://gamma-api.polymarket.com/markets"
HTTP_TIMEOUT_SEC = 20
BALANCE_VERIFY_RETRIES = 4
BALANCE_VERIFY_SLEEP_SEC = 3
MIN_POL_GAS_BALANCE = 0.05
DUST_MIN_RECOVERABLE_USDC = 0.10

# Surcharge CLI (ex. bot 1 temps réel : ``portfolio_rt.sqlite``).
_portfolio_sqlite_override: Optional[Path] = None


def set_portfolio_sqlite_path(path: Optional[Path]) -> None:
    """Fixe la base SQLite (API programmatique ; sinon ``--portfolio-sqlite`` / défaut bot1)."""
    global _portfolio_sqlite_override
    if path is None:
        _portfolio_sqlite_override = None
    else:
        _portfolio_sqlite_override = path.expanduser().resolve()


@dataclass(frozen=True)
class TradePosition:
    market_id: str
    side: str


@dataclass(frozen=True)
class ResolutionState:
    status: str
    winning_side: Optional[str]


def _portfolio_db_path() -> Path:
    if _portfolio_sqlite_override is not None:
        p = _portfolio_sqlite_override
    else:
        p = portfolio_sqlite_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _is_bot2_rt_portfolio_path(path: Path) -> bool:
    return "bot_2_realtime" in str(path).replace("\\", "/")


def _telegram_cashout_context_tag(path: Path) -> str:
    """
    Préfixe visuel pour les alertes de clôture (exit_engine) quand la base est
    ``portfolio_rt.sqlite`` (scalping RT), pas ``portfolio.sqlite`` (long terme).
    """
    s = str(path).replace("\\", "/").lower()
    if "portfolio_rt.sqlite" not in s:
        return ""
    if "bot_2_realtime" in str(path).replace("\\", "/"):
        return "💰 [SCALP RT CASH-OUT · bot2 METAR]"
    return "💰 [SCALP RT CASH-OUT]"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _http_get_json(url: str) -> Any:
    return bot1_http_get_json(url, timeout=float(HTTP_TIMEOUT_SEC))


def _ensure_executed_trades_schema(conn: sqlite3.Connection) -> None:
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
            requested_size REAL,
            matched_size REAL,
            spent_usdc REAL,
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
    if "requested_size" not in col_names:
        conn.execute("ALTER TABLE executed_trades ADD COLUMN requested_size REAL")
    if "matched_size" not in col_names:
        conn.execute("ALTER TABLE executed_trades ADD COLUMN matched_size REAL")
    if "spent_usdc" not in col_names:
        conn.execute("ALTER TABLE executed_trades ADD COLUMN spent_usdc REAL")
    conn.execute(
        """
        UPDATE executed_trades
        SET status = 'OPEN'
        WHERE status IS NULL OR TRIM(status) = ''
        """
    )
    conn.commit()


def _ensure_redeem_attempts_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS redeem_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            side TEXT NOT NULL,
            first_seen_utc TEXT NOT NULL,
            last_attempt_utc TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT,
            UNIQUE(market_id, side)
        )
        """
    )
    conn.commit()


def _open_db() -> sqlite3.Connection:
    """
    Bot 1 batch / RT : ``executed_trades`` + ``redeem_attempts``.
    Bot 2 RT : ``positions`` (``core.bot2_portfolio_schema``) + ``redeem_attempts`` ;
    pas de création de ``executed_trades`` sur une base vide dédiée bot2.
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

    _ensure_redeem_attempts_table(conn)
    return conn


def _load_positions(conn: sqlite3.Connection) -> list[TradePosition]:
    """Paires (market_id, side) distinctes en RESOLVED : ``executed_trades`` et/ou ``positions`` (bot2)."""
    seen: set[tuple[str, str]] = set()
    out: list[TradePosition] = []
    if "executed_trades" in _table_names(conn):
        rows = conn.execute(
            """
            SELECT DISTINCT market_id, recommended_side
            FROM executed_trades
            WHERE status = 'RESOLVED'
              AND market_id IS NOT NULL
              AND market_id != ''
              AND recommended_side IN ('YES', 'NO')
            """
        ).fetchall()
        for market_id, side in rows:
            key = (str(market_id), str(side).upper())
            if key not in seen:
                seen.add(key)
                out.append(TradePosition(market_id=key[0], side=key[1]))
    if "positions" in _table_names(conn):
        rows = conn.execute(
            """
            SELECT DISTINCT market_id, side
            FROM positions
            WHERE UPPER(TRIM(COALESCE(status, ''))) = 'RESOLVED'
              AND market_id IS NOT NULL
              AND market_id != ''
              AND UPPER(TRIM(side)) IN ('YES', 'NO')
            """
        ).fetchall()
        for market_id, side in rows:
            key = (str(market_id), str(side).upper().strip())
            if key not in seen:
                seen.add(key)
                out.append(TradePosition(market_id=key[0], side=key[1]))
    return out


def _get_attempt_status(conn: sqlite3.Connection, market_id: str, side: str) -> Optional[str]:
    row = conn.execute(
        "SELECT status FROM redeem_attempts WHERE market_id = ? AND side = ? LIMIT 1",
        (market_id, side),
    ).fetchone()
    return str(row[0]) if row else None


def _upsert_attempt(
    conn: sqlite3.Connection,
    *,
    market_id: str,
    side: str,
    status: str,
    note: str = "",
) -> None:
    now = _now_utc_iso()
    conn.execute(
        """
        INSERT INTO redeem_attempts (market_id, side, first_seen_utc, last_attempt_utc, status, note)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(market_id, side) DO UPDATE SET
            last_attempt_utc = excluded.last_attempt_utc,
            status = excluded.status,
            note = excluded.note
        """,
        (market_id, side, now, now, status, note[:500]),
    )
    conn.commit()


def _mark_settled_winner(conn: sqlite3.Connection, market_id: str, side: str, settled_at: str) -> int:
    rows = conn.execute(
        """
        SELECT id, station, COALESCE(spent_usdc, best_ask, 0.0)
        FROM executed_trades
        WHERE market_id = ?
          AND recommended_side = ?
          AND status = 'RESOLVED'
        ORDER BY id ASC
        """,
        (market_id, side),
    ).fetchall()
    pos_rows: list[tuple[int, float, float]] = []
    if "positions" in _table_names(conn):
        pos_rows = [
            (int(r[0]), float(r[1]), float(r[2]))
            for r in conn.execute(
                """
                SELECT rowid, price, size
                FROM positions
                WHERE market_id = ?
                  AND UPPER(TRIM(side)) = ?
                  AND UPPER(TRIM(COALESCE(status, ''))) = 'RESOLVED'
                ORDER BY rowid ASC
                """,
                (market_id, side.upper()),
            ).fetchall()
        ]

    if not rows and not pos_rows:
        return 0

    total_spent = float(sum(float(r[2]) for r in rows))
    total_spent += sum(p * s for _, p, s in pos_rows)

    usdc_received = float(getattr(_mark_settled_winner, "_last_usdc_delta", 0.0))
    total_pnl = usdc_received - total_spent
    n_units = len(rows) + len(pos_rows)
    denom = total_spent if total_spent > 0 else float(n_units)

    station = ""
    updated = 0
    assigned_pnl = 0.0
    all_items: list[tuple[str, int, float, Optional[str]]] = []
    for trade_id, station_name, spent_i_raw in rows:
        all_items.append(("et", int(trade_id), float(spent_i_raw), station_name))
    for rowid, p, s in pos_rows:
        all_items.append(("pos", int(rowid), float(p) * float(s), None))

    for i, (kind, tid, spent_i, station_name) in enumerate(all_items):
        if station_name and not station:
            station = str(station_name)
        if i == n_units - 1:
            pnl_i = total_pnl - assigned_pnl
        else:
            weight = (spent_i / denom) if denom > 0 else (1.0 / float(n_units))
            pnl_i = total_pnl * weight
        assigned_pnl += pnl_i
        if kind == "et":
            rc = conn.execute(
                """
                UPDATE executed_trades
                SET status = 'SETTLED',
                    settled_at = ?,
                    pnl = ROUND(?, 6)
                WHERE id = ?
                  AND status = 'RESOLVED'
                """,
                (settled_at, pnl_i, int(tid)),
            ).rowcount
        else:
            rc = conn.execute(
                """
                UPDATE positions
                SET status = 'SETTLED',
                    settled_at = ?,
                    pnl = ROUND(?, 6)
                WHERE rowid = ?
                  AND UPPER(TRIM(COALESCE(status, ''))) = 'RESOLVED'
                """,
                (settled_at, pnl_i, int(tid)),
            ).rowcount
        updated += int(rc or 0)
    conn.commit()
    if station:
        setattr(_mark_settled_winner, "_last_station", station)
    setattr(_mark_settled_winner, "_last_total_pnl", total_pnl)
    return updated


def _mark_settled_loser(conn: sqlite3.Connection, market_id: str, side: str, settled_at: str) -> int:
    total = 0
    if "executed_trades" in _table_names(conn):
        total += int(
            conn.execute(
                """
                UPDATE executed_trades
                SET status = 'SETTLED',
                    settled_at = ?,
                    pnl = ROUND(0.0 - COALESCE(spent_usdc, best_ask, 0.0), 6)
                WHERE market_id = ?
                  AND recommended_side = ?
                  AND status = 'RESOLVED'
                """,
                (settled_at, market_id, side),
            ).rowcount
            or 0
        )
    if "positions" in _table_names(conn):
        total += int(
            conn.execute(
                """
                UPDATE positions
                SET status = 'SETTLED',
                    settled_at = ?,
                    pnl = ROUND(0.0 - (price * size), 6)
                WHERE market_id = ?
                  AND UPPER(TRIM(side)) = ?
                  AND UPPER(TRIM(COALESCE(status, ''))) = 'RESOLVED'
                """,
                (settled_at, market_id, side.upper()),
            ).rowcount
            or 0
        )
    conn.commit()
    return total


def _estimate_recoverable_usdc(conn: sqlite3.Connection, market_id: str, side: str) -> float:
    s = 0.0
    if "executed_trades" in _table_names(conn):
        row = conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE(matched_size, requested_size, 0.0)), 0.0)
            FROM executed_trades
            WHERE market_id = ?
              AND recommended_side = ?
              AND status = 'RESOLVED'
            """,
            (market_id, side),
        ).fetchone()
        s += float(row[0]) if row and row[0] is not None else 0.0
    if "positions" in _table_names(conn):
        row = conn.execute(
            """
            SELECT COALESCE(SUM(price * size), 0.0)
            FROM positions
            WHERE market_id = ?
              AND UPPER(TRIM(side)) = ?
              AND UPPER(TRIM(COALESCE(status, ''))) = 'RESOLVED'
            """,
            (market_id, side.upper()),
        ).fetchone()
        s += float(row[0]) if row and row[0] is not None else 0.0
    return s


def _mark_skipped_dust(conn: sqlite3.Connection, market_id: str, side: str, settled_at: str) -> int:
    total = 0
    if "executed_trades" in _table_names(conn):
        total += int(
            conn.execute(
                """
                UPDATE executed_trades
                SET status = 'SKIPPED_DUST',
                    settled_at = ?,
                    pnl = COALESCE(pnl, 0.0)
                WHERE market_id = ?
                  AND recommended_side = ?
                  AND status = 'RESOLVED'
                """,
                (settled_at, market_id, side),
            ).rowcount
            or 0
        )
    if "positions" in _table_names(conn):
        total += int(
            conn.execute(
                """
                UPDATE positions
                SET status = 'SKIPPED_DUST',
                    settled_at = ?,
                    pnl = COALESCE(pnl, 0.0)
                WHERE market_id = ?
                  AND UPPER(TRIM(side)) = ?
                  AND UPPER(TRIM(COALESCE(status, ''))) = 'RESOLVED'
                """,
                (settled_at, market_id, side.upper()),
            ).rowcount
            or 0
        )
    conn.commit()
    return total


def _fetch_market_question(market_id: str) -> str:
    for q in (
        urllib.parse.urlencode({"condition_ids": market_id, "limit": 1}),
        urllib.parse.urlencode({"id": market_id, "limit": 1}),
    ):
        try:
            data = _http_get_json(f"{GAMMA_URL}?{q}")
            if isinstance(data, list) and data:
                m = data[0]
                return str(m.get("question") or m.get("title") or "").strip()
        except Exception:
            continue
    return ""


def _sum_stake_resolved(conn: sqlite3.Connection, market_id: str, side: str) -> float:
    s = 0.0
    if "executed_trades" in _table_names(conn):
        row = conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE(spent_usdc, 0.0)), 0.0)
            FROM executed_trades
            WHERE market_id = ? AND recommended_side = ? AND status = 'RESOLVED'
            """,
            (market_id, side),
        ).fetchone()
        s += float(row[0] if row and row[0] is not None else 0.0)
    if "positions" in _table_names(conn):
        row = conn.execute(
            """
            SELECT COALESCE(SUM(price * size), 0.0)
            FROM positions
            WHERE market_id = ?
              AND UPPER(TRIM(side)) = ?
              AND UPPER(TRIM(COALESCE(status, ''))) = 'RESOLVED'
            """,
            (market_id, side.upper()),
        ).fetchone()
        s += float(row[0] if row and row[0] is not None else 0.0)
    return s


def _station_for_resolved(conn: sqlite3.Connection, market_id: str, side: str) -> str:
    row = conn.execute(
        """
        SELECT station FROM executed_trades
        WHERE market_id = ? AND recommended_side = ? AND status = 'RESOLVED'
        ORDER BY id ASC
        LIMIT 1
        """,
        (market_id, side),
    ).fetchone()
    return str(row[0]).strip() if row and row[0] else ""


def _fetch_resolution_state(market_id: str) -> ResolutionState:
    query = urllib.parse.urlencode({"condition_ids": market_id, "limit": 1})
    url = f"{GAMMA_URL}?{query}"
    try:
        data = _http_get_json(url)
    except Exception as exc:
        return ResolutionState(status="API_ERROR", winning_side=None if exc else None)
    if not isinstance(data, list) or not data:
        return ResolutionState(status="NOT_FOUND", winning_side=None)
    m = data[0]
    raw = m.get("outcomePrices") or []
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
        return ResolutionState(status="PENDING", winning_side=None)
    p_yes = prices[0]
    if p_yes >= 0.99:
        return ResolutionState(status="RESOLVED", winning_side="YES")
    if p_yes <= 0.01:
        return ResolutionState(status="RESOLVED", winning_side="NO")
    if bool(m.get("closed", False)):
        return ResolutionState(status="CANCELLED", winning_side=None)
    return ResolutionState(status="PENDING", winning_side=None)


def run(dry_run: bool = False) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    cashout_tag = _telegram_cashout_context_tag(_portfolio_db_path())
    pol_balance = float(get_pol_balance())
    if pol_balance < MIN_POL_GAS_BALANCE and not dry_run:
        send_alert(
            (
                f"Urgent: Rechargez POL. Solde gaz trop bas pour settlement "
                f"({pol_balance:.4f} POL < {MIN_POL_GAS_BALANCE:.2f} POL)."
            ),
            emoji="⛽",
        )
        LOG.error(
            "Coupe-circuit gas activé: pol_balance=%.6f < %.2f, arrêt du settler.",
            pol_balance,
            MIN_POL_GAS_BALANCE,
        )
        return 0

    conn = _open_db()
    processed = 0
    settled = 0
    skipped = 0
    try:
        positions = _load_positions(conn)
        if not positions:
            LOG.info("Aucune position RESOLVED à traiter pour settlement.")
            return 0
        for pos in positions:
            prev = _get_attempt_status(conn, pos.market_id, pos.side)
            if prev in ("SUCCESS", "NOT_WINNER", "CANCELLED", "SKIPPED_DUST"):
                skipped += 1
                continue
            state = _fetch_resolution_state(pos.market_id)
            if state.status in ("API_ERROR", "NOT_FOUND", "PENDING"):
                _upsert_attempt(
                    conn,
                    market_id=pos.market_id,
                    side=pos.side,
                    status=state.status,
                    note="market unresolved or api unavailable",
                )
                skipped += 1
                continue
            if state.status == "CANCELLED":
                stake = _sum_stake_resolved(conn, pos.market_id, pos.side)
                station = _station_for_resolved(conn, pos.market_id, pos.side)
                question = _fetch_market_question(pos.market_id) or (
                    f"(marché {pos.market_id[:16]}…)"
                )
                changed = _mark_settled_loser(
                    conn,
                    market_id=pos.market_id,
                    side=pos.side,
                    settled_at=_now_utc_iso(),
                )
                if changed > 0:
                    settled += changed
                    alert_bot1_position_close(
                        title="Fermeture — marché annulé",
                        question=question,
                        side=pos.side,
                        station=station,
                        pnl_usdc=-stake,
                        stake_usdc=stake,
                        detail="Marché annulé ; mise comptabilisée comme perte.",
                        emoji="⚠️",
                    )
                _upsert_attempt(
                    conn,
                    market_id=pos.market_id,
                    side=pos.side,
                    status="CANCELLED",
                    note="market cancelled, no redeem side winner",
                )
                skipped += 1
                continue
            assert state.status == "RESOLVED"
            if state.winning_side != pos.side:
                stake = _sum_stake_resolved(conn, pos.market_id, pos.side)
                station = _station_for_resolved(conn, pos.market_id, pos.side)
                question = _fetch_market_question(pos.market_id) or (
                    f"(marché {pos.market_id[:16]}…)"
                )
                changed = _mark_settled_loser(
                    conn,
                    market_id=pos.market_id,
                    side=pos.side,
                    settled_at=_now_utc_iso(),
                )
                if changed > 0:
                    settled += changed
                    alert_bot1_position_close(
                        title="Fermeture — position perdante",
                        question=question,
                        side=pos.side,
                        station=station,
                        pnl_usdc=-stake,
                        stake_usdc=stake,
                        detail=f"Côté gagnant du marché : {state.winning_side}",
                        emoji="🔴",
                        cashout_context_tag=cashout_tag,
                    )
                _upsert_attempt(
                    conn,
                    market_id=pos.market_id,
                    side=pos.side,
                    status="NOT_WINNER",
                    note=f"winning_side={state.winning_side}",
                )
                skipped += 1
                continue
            recoverable_usdc = _estimate_recoverable_usdc(conn, pos.market_id, pos.side)
            if recoverable_usdc < DUST_MIN_RECOVERABLE_USDC:
                station = _station_for_resolved(conn, pos.market_id, pos.side)
                question = _fetch_market_question(pos.market_id) or (
                    f"(marché {pos.market_id[:16]}…)"
                )
                changed = _mark_skipped_dust(
                    conn,
                    market_id=pos.market_id,
                    side=pos.side,
                    settled_at=_now_utc_iso(),
                )
                _upsert_attempt(
                    conn,
                    market_id=pos.market_id,
                    side=pos.side,
                    status="SKIPPED_DUST",
                    note=(
                        f"recoverable={recoverable_usdc:.6f} "
                        f"threshold={DUST_MIN_RECOVERABLE_USDC:.2f}"
                    ),
                )
                if changed > 0:
                    alert_bot1_position_close(
                        title="Fermeture — redeem non tenté (dust)",
                        question=question,
                        side=pos.side,
                        station=station,
                        pnl_usdc=0.0,
                        stake_usdc=recoverable_usdc,
                        detail=(
                            f"Montant récupérable estimé {recoverable_usdc:.4f} USDC "
                            f"&lt; seuil {DUST_MIN_RECOVERABLE_USDC:.2f} USDC."
                        ),
                        emoji="📎",
                        cashout_context_tag=cashout_tag,
                    )
                skipped += max(1, changed)
                LOG.info(
                    "Dust skip market_id=%s side=%s recoverable=%.6f threshold=%.2f rows=%s",
                    pos.market_id,
                    pos.side,
                    recoverable_usdc,
                    DUST_MIN_RECOVERABLE_USDC,
                    changed,
                )
                continue
            processed += 1
            details = redeem_position_detailed(
                condition_id=pos.market_id,
                side=pos.side,
                dry_run=dry_run,
            )
            usdc_after = float(get_usdc_balance())
            usdc_delta = usdc_after - float(details.usdc_before)
            result = details.result
            if result == RedeemResult.DRY_RUN:
                _upsert_attempt(
                    conn,
                    market_id=pos.market_id,
                    side=pos.side,
                    status=result.value,
                    note="dry-run",
                )
                LOG.info(
                    "Redeem DRY_RUN market_id=%s side=%s usdc_before=%.6f usdc_after=%.6f",
                    pos.market_id,
                    pos.side,
                    details.usdc_before,
                    usdc_after,
                )
                continue

            if result == RedeemResult.SUCCESS and usdc_delta <= 0:
                for _ in range(BALANCE_VERIFY_RETRIES):
                    time.sleep(BALANCE_VERIFY_SLEEP_SEC)
                    refreshed = float(get_usdc_balance())
                    usdc_delta = refreshed - float(details.usdc_before)
                    usdc_after = refreshed
                    if usdc_delta > 0:
                        break

            if result == RedeemResult.SUCCESS and usdc_delta > 0:
                stake_before = _sum_stake_resolved(conn, pos.market_id, pos.side)
                question = _fetch_market_question(pos.market_id) or (
                    f"(marché {pos.market_id[:16]}…)"
                )
                _upsert_attempt(
                    conn,
                    market_id=pos.market_id,
                    side=pos.side,
                    status=result.value,
                    note=(
                        f"redeem confirmed tx={details.tx_hash or '-'} "
                        f"event={int(bool(details.payout_event_found))} "
                        f"usdc_delta={usdc_delta:.6f}"
                    ),
                )
                setattr(_mark_settled_winner, "_last_usdc_delta", usdc_delta)
                changed = _mark_settled_winner(
                    conn,
                    market_id=pos.market_id,
                    side=pos.side,
                    settled_at=_now_utc_iso(),
                )
                settled += changed
                station = str(getattr(_mark_settled_winner, "_last_station", "")).strip() or "N/A"
                total_pnl = float(getattr(_mark_settled_winner, "_last_total_pnl", 0.0))
                tx_short = (
                    (details.tx_hash[:18] + "…")
                    if details.tx_hash and len(details.tx_hash) > 20
                    else (details.tx_hash or "-")
                )
                alert_bot1_position_close(
                    title="Fermeture — gain encaissé (redeem)",
                    question=question,
                    side=pos.side,
                    station=station,
                    pnl_usdc=total_pnl,
                    stake_usdc=stake_before,
                    detail=(
                        f"USDC reçus (delta wallet) ~{usdc_delta:.2f} · "
                        f"solde ~{usdc_after:.2f} USDC · tx {tx_short}"
                    ),
                    emoji="🟢",
                    cashout_context_tag=cashout_tag,
                )
                LOG.info(
                    "Redeem SUCCESS market_id=%s side=%s tx=%s event=%s "
                    "usdc_delta=%.6f settled_rows=%s",
                    pos.market_id,
                    pos.side,
                    details.tx_hash[:20] + "..." if details.tx_hash else "-",
                    details.payout_event_found,
                    usdc_delta,
                    changed,
                )
            elif result == RedeemResult.SUCCESS:
                verify_note = (
                    f"verify_failed tx={details.tx_hash or '-'} "
                    f"event={int(bool(details.payout_event_found))} "
                    f"usdc_delta={usdc_delta:.6f}"
                )
                _upsert_attempt(
                    conn,
                    market_id=pos.market_id,
                    side=pos.side,
                    status="VERIFY_FAILED",
                    note=verify_note,
                )
                LOG.warning(
                    "Redeem SUCCESS mais vérification ambiguë market_id=%s side=%s "
                    "tx=%s event=%s delta=%.6f -> status attempt=VERIFY_FAILED",
                    pos.market_id,
                    pos.side,
                    details.tx_hash[:20] + "..." if details.tx_hash else "-",
                    details.payout_event_found,
                    usdc_delta,
                )
                alert_error(
                    f"Redeem OK mais solde USDC inchangé (vérif manuelle). "
                    f"market={pos.market_id[:24]}… side={pos.side} delta={usdc_delta:.6f}"
                )
            else:
                _upsert_attempt(
                    conn,
                    market_id=pos.market_id,
                    side=pos.side,
                    status=result.value,
                    note=(
                        f"redeem failed tx={details.tx_hash or '-'} "
                        f"event={int(bool(details.payout_event_found))} "
                        f"error={details.error[:200]}"
                    ),
                )
                LOG.warning(
                    "Redeem %s market_id=%s side=%s tx=%s event=%s usdc_delta=%.6f",
                    result.value,
                    pos.market_id,
                    pos.side,
                    details.tx_hash[:20] + "..." if details.tx_hash else "-",
                    details.payout_event_found,
                    usdc_delta,
                )
                alert_error(
                    f"Redeem {result.value} market={pos.market_id[:24]}… "
                    f"side={pos.side} err={details.error[:120]}"
                )
    finally:
        conn.close()
    LOG.info(
        "exit_engine terminé: processed=%s settled=%s skipped=%s",
        processed,
        settled,
        skipped,
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Settler: traite les positions RESOLVED, redeem, vérifie USDC et marque SETTLED.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemples:\n"
            "  %(prog)s --portfolio-sqlite data/bots/realtime/portfolio_rt.sqlite\n"
            "  %(prog)s --dry-run --portfolio-sqlite data/bots/realtime/portfolio_rt.sqlite\n"
            "  %(prog)s --portfolio-sqlite data/bots/bot_2_realtime/portfolio_rt.sqlite\n"
            "\n"
            "Sans --portfolio-sqlite : utilise PORTFOLIO_SQLITE_PATH / core.bot1_data_paths (batch bot1).\n"
            "Bot 2 RT : même script, table ``positions`` (RESOLVED -> redeem -> SETTLED)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulation sans transaction on-chain.",
    )
    parser.add_argument(
        "--portfolio-sqlite",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Base SQLite (executed_trades et/ou positions en RESOLVED). "
            "Bot1 RT : ex. data/bots/realtime/portfolio_rt.sqlite. "
            "Bot2 RT METAR : ex. data/bots/bot_2_realtime/portfolio_rt.sqlite. "
            "Défaut sans ce flag : portfolio batch (PORTFOLIO_SQLITE_PATH)."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    set_portfolio_sqlite_path(args.portfolio_sqlite)
    raise SystemExit(run_guarded("bot1.exit_engine", lambda: run(dry_run=bool(args.dry_run))))
