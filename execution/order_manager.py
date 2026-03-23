"""
Gestion des ordres sur Polymarket CLOB.
DRY_RUN=1 par défaut — aucun ordre réel tant que pas validé.
Chaque trade est enregistré dans data/logs/trades.json (survit aux crashes).
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone
from typing import Optional
from core.client import get_client
from core.models import TradeSignal, Order
from config.settings import DRY_RUN, FEE_BPS
from monitoring.logger import log
from monitoring.pnl_tracker import record_trade


def _already_open(condition_id: str) -> bool:
    """Vérifie si une position est déjà ouverte pour ce marché."""
    from monitoring.pnl_tracker import get_open_positions
    return any(p.get("condition_id") == condition_id for p in get_open_positions())


def place_order(signal: TradeSignal) -> Optional[Order]:
    """Place un ordre réel ou simulé selon DRY_RUN."""

    if _already_open(signal.market.condition_id):
        log.info(f"  ↩ Position déjà ouverte — skip | {signal.market.question[:50]}")
        return None

    token = signal.market.yes_token if signal.side == "YES" else signal.market.no_token
    if token is None:
        log.error("Token introuvable pour ce signal")
        return None

    entry_price = token.price
    end_date    = signal.market.end_date
    end_str     = end_date.strftime("%Y-%m-%d") if end_date else None

    order = Order(
        condition_id = signal.market.condition_id,
        token_id     = signal.token_id,
        side         = "BUY",
        size_usd     = signal.kelly_stake_usd,
        price        = entry_price,
    )

    if DRY_RUN:
        order_id       = f"DRY_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        order.status   = "dry_run"
        order.order_id = order_id

        record_trade(
            condition_id = signal.market.condition_id,
            token_id     = signal.token_id,
            question     = signal.market.question,
            side         = signal.side,
            size_usd     = signal.kelly_stake_usd,
            price        = entry_price,
            edge         = signal.edge,
            our_prob     = signal.signal.our_yes_prob,
            market_prob  = signal.market.market_yes_prob or 0.5,
            confidence   = signal.signal.confidence,
            end_date     = end_str,
            dry_run      = True,
            order_id     = order_id,
        )

        log.info(
            f"[DRY-RUN] Ordre simulé | {signal.side} | ${signal.kelly_stake_usd:.2f} "
            f"@ {entry_price:.3f} | edge={signal.edge*100:.1f}% | "
            f"expire={end_str} | {signal.market.question[:50]}"
        )
        return order

    # ── LIVE ──────────────────────────────────────────────────────────────────
    try:
        client = get_client()
        resp = client.create_order({
            "token_id":     signal.token_id,
            "price":        entry_price,
            "side":         "BUY",
            "size":         signal.kelly_stake_usd,
            # FEE_BPS depuis settings — 0 si Polymarket CLOB est sans frais
            "fee_rate_bps": FEE_BPS,
        })
        order.order_id = resp.get("orderID") or resp.get("order_id")
        order.status   = "filled" if resp.get("status") == "matched" else "pending"

        record_trade(
            condition_id = signal.market.condition_id,
            token_id     = signal.token_id,
            question     = signal.market.question,
            side         = signal.side,
            size_usd     = signal.kelly_stake_usd,
            price        = entry_price,
            edge         = signal.edge,
            our_prob     = signal.signal.our_yes_prob,
            market_prob  = signal.market.market_yes_prob or 0.5,
            confidence   = signal.signal.confidence,
            end_date     = end_str,
            dry_run      = False,
            order_id     = order.order_id,
        )

        log.info(
            f"✅ Ordre LIVE placé | {order.order_id} | {signal.side} "
            f"| ${signal.kelly_stake_usd:.2f} @ {entry_price:.3f} "
            f"| {signal.market.question[:50]}"
        )
        return order

    except Exception as e:
        log.error(f"❌ Erreur ordre LIVE : {e}")
        order.status = "failed"
        return order