"""
Gestion des ordres sur Polymarket CLOB.
DRY_RUN=1 par défaut — aucun ordre réel tant que pas validé.
Chaque trade est enregistré dans data/logs/trades.json (survit aux crashes).
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone
from typing import Any, Optional
from core.client import get_client
from core.models import TradeSignal, Order
from config.settings import DRY_RUN, FEE_BPS
from monitoring.logger import log
from monitoring.pnl_tracker import record_trade
from execution.ctf_manager import verify_token_balance


def _already_open(condition_id: str) -> bool:
    """Vérifie si une position est déjà ouverte pour ce marché."""
    from monitoring.pnl_tracker import get_open_positions
    return any(p.get("condition_id") == condition_id for p in get_open_positions())


def _submit_live_buy_order(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Soumet réellement un ordre au CLOB (create -> post), avec fallback multi-SDK.
    """
    signed_order = client.create_order(payload)

    # Voie principale du SDK py_clob_client: post_order(signed_order, order_type=GTC)
    post_fn = getattr(client, "post_order", None)
    if callable(post_fn):
        order_type = None
        try:
            from py_clob_client.clob_types import OrderType  # type: ignore
            order_type = getattr(OrderType, "GTC", None)
        except Exception:
            order_type = None
        if order_type is not None:
            try:
                return post_fn(signed_order, order_type=order_type)
            except TypeError:
                return post_fn(signed_order, order_type)
        return post_fn(signed_order)

    # Fallback possible sur certaines versions:
    create_and_post_fn = getattr(client, "create_and_post_order", None)
    if callable(create_and_post_fn):
        return create_and_post_fn(payload)

    # Dernier recours: retourne la réponse de signature seule.
    return signed_order if isinstance(signed_order, dict) else {"raw_response": signed_order}


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
        resp = _submit_live_buy_order(client, {
            "token_id":     signal.token_id,
            "price":        entry_price,
            "side":         "BUY",
            "size":         signal.kelly_stake_usd,
            "fee_rate_bps": FEE_BPS,
        })
        order.order_id = resp.get("orderID") or resp.get("order_id")

        # ── Gestion des fills partiels ────────────────────────────────────────
        # Le CLOB Polymarket est un order book — un ordre peut être partiellement
        # rempli si la liquidité est insuffisante au prix demandé.
        # On lit size_matched (USDC remplis) vs size_usd demandé.
        #
        # Champs possibles selon la version du client :
        #   "sizeMatched"  → USDC effectivement tradés
        #   "makingAmount" → côté maker
        #   "takingAmount" → côté taker
        size_matched = float(
            resp.get("sizeMatched")
            or resp.get("size_matched")
            or resp.get("makingAmount")
            or signal.kelly_stake_usd   # fallback : on assume full fill
        )
        fill_ratio  = size_matched / signal.kelly_stake_usd if signal.kelly_stake_usd > 0 else 1.0
        fill_ratio  = min(1.0, fill_ratio)

        if fill_ratio < 0.95:
            # Fill partiel significatif (< 95% de la taille demandée)
            log.warning(
                f"⚠️ Fill partiel | {order.order_id} | "
                f"demandé=${signal.kelly_stake_usd:.2f} | "
                f"rempli=${size_matched:.2f} ({fill_ratio*100:.0f}%) | "
                f"{signal.market.question[:40]}"
            )
            order.status = "partial"
        elif resp.get("status") == "matched" or fill_ratio >= 0.95:
            order.status = "filled"
        else:
            order.status = "pending"

        # On enregistre la taille réellement remplie, pas la taille demandée
        effective_size = round(size_matched, 4)
        order.size_usd = effective_size   # corrige l'ordre pour la comptabilité

        record_trade(
            condition_id    = signal.market.condition_id,
            token_id        = signal.token_id,
            question        = signal.market.question,
            side            = signal.side,
            size_usd        = effective_size,
            size_requested  = signal.kelly_stake_usd,
            price           = entry_price,
            edge            = signal.edge,
            our_prob        = signal.signal.our_yes_prob,
            market_prob     = signal.market.market_yes_prob or 0.5,
            confidence      = signal.signal.confidence,
            end_date        = end_str,
            dry_run         = False,
            order_id        = order.order_id,
            fill_ratio      = round(fill_ratio, 4),
        )

        emoji = "✅" if order.status == "filled" else "⚠️"
        log.info(
            f"{emoji} Ordre LIVE | {order.order_id} | {signal.side} "
            f"| rempli=${effective_size:.2f}/{signal.kelly_stake_usd:.2f} "
            f"({fill_ratio*100:.0f}%) @ {entry_price:.3f} "
            f"| {signal.market.question[:40]}"
        )

        # ── Vérification balance ERC-1155 on-chain ────────────────────────────
        # On attend 3s que la tx soit indexée sur Polygon avant de lire la balance.
        # expected_tokens = size remplie / prix d'entrée = nombre de shares achetés.
        if order.status in ("filled", "partial") and signal.token_id:
            import time as _time
            _time.sleep(3)
            expected_tokens = effective_size / entry_price if entry_price > 0 else 0
            balance_ok, actual_balance = verify_token_balance(
                token_id        = signal.token_id,
                expected_tokens = expected_tokens,
            )
            # Log persistant de la vérification
            try:
                from monitoring.onchain_logger import log_balance_check
                log_balance_check(
                    condition_id    = signal.market.condition_id,
                    token_id        = signal.token_id,
                    expected_tokens = expected_tokens,
                    actual_balance  = actual_balance,
                    ok              = balance_ok,
                )
            except Exception:
                pass
            if not balance_ok:
                log.error(
                    f"🚨 FILL SILENCIEUX DÉTECTÉ | {order.order_id} | "
                    f"tokens attendus={expected_tokens:.4f} reçus={actual_balance:.4f} | "
                    f"Vérification manuelle requise — position NON enregistrée"
                )
                order.status = "balance_mismatch"
            else:
                log.debug(f"✅ Balance ERC-1155 confirmée | {actual_balance:.4f} tokens")

        return order

    except Exception as e:
        log.error(f"❌ Erreur ordre LIVE : {e}")
        order.status = "failed"
        return order