"""
Schéma SQLite du portefeuille **temps réel** (secours / audit).

- ``executed_trades`` : aligné sur ``bots.bot1.execution.finance_engine`` (OPEN →
  RESOLVED → SETTLED), utilisé par ``finance_engine_rt`` et la chaîne settlement.
- ``rt_orders`` : journal d’ordres CLOB (granularité ordre ; peut être alimenté
  ensuite sans dupliquer la logique d’exécution).
- Vues ``positions_open``, ``positions_resolved``, ``positions_settled`` : filtres
  sur ``executed_trades.status`` pour requêtes et exports.

``ensure_portfolio_rt_extensions`` complète une base déjà initialisée par
``_open_portfolio_db`` (même table ``executed_trades``).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def ensure_portfolio_rt_extensions(conn: sqlite3.Connection) -> None:
    """Ajoute ``rt_orders`` et les vues positions après ``executed_trades``."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rt_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placed_at_utc TEXT NOT NULL,
            market_id TEXT NOT NULL,
            forecast_cycle_utc TEXT NOT NULL DEFAULT '',
            recommended_side TEXT NOT NULL,
            clob_order_id TEXT,
            status TEXT NOT NULL,
            requested_size REAL,
            matched_size REAL,
            raw_response_json TEXT
        );

        CREATE INDEX IF NOT EXISTS ix_rt_orders_market
        ON rt_orders (market_id, forecast_cycle_utc);

        CREATE INDEX IF NOT EXISTS ix_rt_orders_clob_id
        ON rt_orders (clob_order_id);

        DROP VIEW IF EXISTS positions_open;
        CREATE VIEW positions_open AS
        SELECT *
        FROM executed_trades
        WHERE UPPER(TRIM(COALESCE(status, ''))) = 'OPEN';

        DROP VIEW IF EXISTS positions_resolved;
        CREATE VIEW positions_resolved AS
        SELECT *
        FROM executed_trades
        WHERE UPPER(TRIM(COALESCE(status, ''))) = 'RESOLVED';

        DROP VIEW IF EXISTS positions_settled;
        CREATE VIEW positions_settled AS
        SELECT *
        FROM executed_trades
        WHERE UPPER(TRIM(COALESCE(status, ''))) = 'SETTLED';
        """
    )
    conn.commit()


def init_portfolio_rt_file(path: Path) -> None:
    """
    Crée le fichier SQLite et applique les extensions RT (sans passer par
    ``finance_engine``). Utile pour initialiser une base vide avant premier run.

    Pour rester aligné sur le moteur batch, la table ``executed_trades`` doit
    rester identique à ``_open_portfolio_db`` : on importe localement au moment
    de l’appel pour éviter une duplication de DDL.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    from bots.bot1.execution import finance_engine as fe

    conn = fe._open_portfolio_db(path)
    try:
        ensure_portfolio_rt_extensions(conn)
    finally:
        conn.close()
