"""
Portefeuille SQLite **bot 2 temps réel** (``data/bots/bot_2_realtime/portfolio_rt.sqlite``),
distinct du bot 1 long-terme et du bot 1 RT (``data/bots/realtime/``).

Table ``positions`` : journal minimal pour le module de settlement (statut **OPEN** à l’entrée).
"""
from __future__ import annotations

import sqlite3


def ensure_bot2_positions_table(conn: sqlite3.Connection) -> None:
    """Crée ``positions`` et un index léger si la table n’existe pas encore."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS positions (
            market_id TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            size REAL NOT NULL,
            timestamp TEXT NOT NULL,
            status TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS ix_bot2_positions_market_status
        ON positions (market_id, status);
        """
    )
    cols = {str(c[1]) for c in conn.execute("PRAGMA table_info(positions)").fetchall()}
    if "pnl" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN pnl REAL")
    if "settled_at" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN settled_at TEXT")
    conn.commit()


def insert_open_position(
    conn: sqlite3.Connection,
    *,
    market_id: str,
    side: str,
    price: float,
    size: float,
    timestamp_iso_utc: str,
) -> None:
    """Insère une ligne avec ``status='OPEN'``."""
    conn.execute(
        """
        INSERT INTO positions (market_id, side, price, size, timestamp, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (market_id, side, price, size, timestamp_iso_utc, "OPEN"),
    )
