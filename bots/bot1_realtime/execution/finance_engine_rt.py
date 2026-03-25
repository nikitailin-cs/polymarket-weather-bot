#!/usr/bin/env python3
"""
Moteur financier **bot 1 temps réel / scalping** (exécuteur haute fréquence).

- **Manifeste** ``realtime_signals_meta.json`` : refus si ``status != ok`` ou si l’âge du
  signal dépasse ``FINANCE_RT_MAX_SIGNAL_AGE_SEC`` (défaut **60** s) — pas de scalping
  sur donnée périmée.
- **Carnet L2** : lecture REST ``/book`` (CLOB) ; agrégation des asks pour le meilleur
  prix ; optionnellement croisement avec le client ``py_clob_client`` si disponible.
- **Sizing** : plafond ``FINANCE_RT_*`` + solde USDC total (bridgé + natif) du **proxy /
  funder** (``get_usdc_balances``) moins ``FINANCE_RT_USDC_RESERVE``.
- **Scooping** : ordre limite d’achat YES au **best ask** (normalisé au tick), taille
  ``min(budget, liquidité au niveau, pouvoir d’achat / prix)``, prix ≤
  ``FINANCE_RT_MAX_BEST_ASK_USDC`` (défaut **0.99**).
- **Persistance** : ``_record_trade`` → ``portfolio_rt.sqlite`` (``executed_trades``,
  statut **OPEN** par défaut).

Dry-run : ``FINANCE_RT_DRY_RUN`` ou ``FINANCE_ENGINE_RT_DRY_RUN`` (sinon suit ``DRY_RUN``).
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

from bots.bot1.execution import finance_engine as fe  # noqa: E402
from config.settings import MAX_BET_USD  # noqa: E402
from core.bot1_realtime_data_paths import (  # noqa: E402
    portfolio_rt_sqlite_path,
    realtime_signals_meta_path,
    realtime_signals_path,
)
from core.bot_rt_env import apply_rt_signing_env  # noqa: E402
from core.portfolio_rt_schema import ensure_portfolio_rt_extensions  # noqa: E402
from core.polymarket_proxy import apply_proxy_env_for_clob  # noqa: E402
from monitoring.telegram_alert import alert_rt_scalp_success  # noqa: E402

LOG = logging.getLogger("finance_engine_rt")


def _finance_rt_is_dry_run() -> bool:
    v = (os.getenv("FINANCE_RT_DRY_RUN") or os.getenv("FINANCE_ENGINE_RT_DRY_RUN") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    from config.settings import DRY_RUN

    return DRY_RUN


def _parse_iso_utc(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _rt_max_signal_age_sec() -> float:
    """Scalping : défaut 60 s (anti-stale)."""
    try:
        return float(os.getenv("FINANCE_RT_MAX_SIGNAL_AGE_SEC", "60"))
    except ValueError:
        return 60.0


def _meta_rt_freshness_allows_trading(meta: dict[str, Any]) -> bool:
    """
    Fenêtre stricte côté exécuteur : ``FINANCE_RT_MAX_SIGNAL_AGE_SEC`` (défaut 60 s),
    indépendamment du TTL du manifeste producteur.
    """
    max_age = _rt_max_signal_age_sec()
    if max_age < 0:
        return True
    now = datetime.now(timezone.utc)
    raw_u = meta.get("generated_at_unix")
    if type(raw_u) in (int, float):
        gen_ts = float(raw_u)
        age_sec = now.timestamp() - gen_ts
        if age_sec > max_age:
            LOG.error(
                "RT STOP — signaux trop vieux: generated_at_unix=%s (âge %.1fs > max %ss)",
                raw_u,
                age_sec,
                int(max_age),
            )
            return False
        return True
    raw_gen = meta.get("generated_at_utc")
    gen_at = _parse_iso_utc(raw_gen)
    if gen_at is None:
        LOG.error(
            "RT bloqué — manifeste ok mais generated_at_utc invalide ou absent (%r)",
            raw_gen,
        )
        return False
    if (now - gen_at) > timedelta(seconds=max_age):
        LOG.error(
            "RT STOP — signaux trop vieux: generated_at_utc=%s (limite %ss)",
            raw_gen,
            int(max_age),
        )
        return False
    return True


def _manifest_rt_allows_trading() -> bool:
    meta_path = realtime_signals_meta_path()
    if not meta_path.is_file():
        LOG.warning(
            "Manifeste RT absent (%s) — validation désactivée (legacy)",
            meta_path,
        )
        return True
    try:
        meta: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOG.error("Manifeste RT illisible (%s): %s", meta_path, exc)
        return False
    status = str(meta.get("status", "")).strip()
    if status != "ok":
        LOG.error(
            "Trading RT bloqué — status=%r raison=%s",
            status,
            meta.get("reason", ""),
        )
        return False
    if not _meta_rt_freshness_allows_trading(meta):
        return False
    return True


def _is_scalp_yes_at_threshold(sig: fe.TradingSignal) -> bool:
    """p_win=1 + YES + ``binary_state`` absent ou ``attack`` (phase 3 sniper)."""
    if sig.recommended_side != "YES":
        return False
    if float(sig.p_win) < 1.0 - 1e-9:
        return False
    bs = (getattr(sig, "binary_state", "") or "").strip().lower()
    if bs and bs != "attack":
        return False
    return True


def _rt_max_best_ask() -> float:
    try:
        return float(os.getenv("FINANCE_RT_MAX_BEST_ASK_USDC", "0.99"))
    except ValueError:
        return 0.99


def _rt_order_budget_usdc(order_size_arg: Optional[float]) -> float:
    if order_size_arg is not None:
        raw = float(order_size_arg)
    else:
        try:
            raw = float(os.getenv("FINANCE_RT_ORDER_SIZE_USDC", "15.0"))
        except ValueError:
            raw = 15.0
    try:
        cap = float(os.getenv("FINANCE_RT_MAX_BET_USDC", str(max(float(MAX_BET_USD) * 2.5, 25.0))))
    except ValueError:
        cap = max(float(MAX_BET_USD) * 2.5, 25.0)
    return max(0.0, min(raw, cap))


def _rt_buying_power_usdc() -> float:
    """
    USDC disponibles pour ordres (proxy / funder Polymarket), moins réserve.
    Retour ``math.inf`` si lecture impossible (fallback : budget config seul).
    """
    try:
        from execution.ctf_manager import get_usdc_balances  # noqa: WPS433
        from core.client import resolve_clob_funder  # noqa: WPS433

        addr = resolve_clob_funder()
        if not addr:
            LOG.warning("RT sizing: pas d'adresse funder — budget config seul")
            return math.inf
        b = get_usdc_balances(addr)
        total = float(b.get("total", 0.0))
        try:
            reserve = float(os.getenv("FINANCE_RT_USDC_RESERVE", "0") or "0")
        except ValueError:
            reserve = 0.0
        out = max(0.0, total - reserve)
        LOG.info("RT sizing: USDC funder=%s total=%.4f (après réserve %.4f) → %.4f", addr[:10], total, reserve, out)
        return out
    except Exception as exc:
        LOG.warning("RT sizing: lecture USDC impossible (%s) — budget config seul", exc)
        return math.inf


def _log_l2_yes_snapshot(token_id: str) -> None:
    book = fe.fetch_clob_book(token_id)
    if not book:
        LOG.warning("L2: carnet vide ou indisponible token_id=%s…", token_id[:16])
        return
    levels = fe.ask_levels_from_book(book, max_levels=8)
    preview = levels[:5] if levels else []
    tick = book.get("tick_size") or book.get("tickSize")
    LOG.info(
        "L2 YES token=%s… tick=%s niveaux ask (prix, size) top: %s",
        token_id[:12],
        tick,
        preview,
    )
    if os.getenv("FINANCE_RT_TRY_SDK_ORDERBOOK", "").strip().lower() in ("1", "true", "yes"):
        try:
            client = fe._get_clob_client()
            for name in ("get_order_book", "get_orderbook", "get_book"):
                fn = getattr(client, name, None)
                if callable(fn):
                    try:
                        sdk_ob = fn(token_id)
                        LOG.info("py_clob_client.%s: type=%s", name, type(sdk_ob).__name__)
                    except Exception as e:
                        LOG.debug("SDK %s: %s", name, e)
                    break
        except Exception as exc:
            LOG.debug("Client CLOB indisponible pour snapshot SDK: %s", exc)


def run(order_size_usdc: Optional[float] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    load_dotenv(dotenv_path=_ROOT / ".env", override=False)
    apply_rt_signing_env()
    apply_proxy_env_for_clob()

    dry = _finance_rt_is_dry_run()
    if dry:
        LOG.info(
            "Mode DRY-RUN (finance_rt) — aucun ordre CLOB, aucune ligne portfolio_rt.sqlite"
        )

    max_ask = _rt_max_best_ask()
    if not (0.0 < max_ask <= 1.0):
        LOG.error("FINANCE_RT_MAX_BEST_ASK_USDC invalide (attendu ]0,1], got %s)", max_ask)
        return 1

    budget_cfg = _rt_order_budget_usdc(order_size_usdc)
    buying_power = _rt_buying_power_usdc() if not dry else math.inf
    remaining_cash = buying_power
    LOG.info(
        "Scalping RT — budget_config=%.2f USDC, buying_power=%s, "
        "prix max ask=%.4f (FINANCE_RT_MAX_BEST_ASK_USDC), max_age_signal=%.0fs",
        budget_cfg,
        "inf" if buying_power is math.inf else f"{buying_power:.4f}",
        max_ask,
        _rt_max_signal_age_sec(),
    )

    if not _manifest_rt_allows_trading():
        LOG.info("Arrêt: manifeste temps réel non valide ou signal trop vieux.")
        return 0

    path_sig = realtime_signals_path()
    try:
        signals = fe._load_signals(path_sig)
    except Exception as exc:
        LOG.error("Lecture signaux RT: %s", exc)
        return 1

    scalp_signals = [s for s in signals if _is_scalp_yes_at_threshold(s)]
    if not scalp_signals:
        LOG.info(
            "Aucun signal scalping (p_win=1.0 & YES & attack) — total signaux=%s",
            len(signals),
        )
        return 0

    db_path = portfolio_rt_sqlite_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = fe._open_portfolio_db(db_path)
    ensure_portfolio_rt_extensions(conn)

    executed = 0
    dry_simulated = 0
    skipped = 0
    min_notional = float(os.getenv("FINANCE_RT_MIN_ORDER_NOTIONAL_USDC", "1.0"))

    try:
        for sig in scalp_signals:
            if fe._already_traded_for_cycle(conn, sig.market_id, sig.forecast_cycle_utc):
                skipped += 1
                LOG.info(
                    "SKIP déjà tradé RT market_id=%s cycle=%s",
                    sig.market_id,
                    sig.forecast_cycle_utc,
                )
                continue

            tokens = fe._resolve_tokens_for_market(sig.market_id)
            if tokens is None:
                skipped += 1
                LOG.warning("SKIP token_ids introuvables market_id=%s", sig.market_id)
                continue

            token_id = tokens.yes_token_id
            _log_l2_yes_snapshot(token_id)

            book_top = fe._fetch_book_top(token_id)
            if book_top is None:
                skipped += 1
                LOG.warning("SKIP best ask indisponible market_id=%s", sig.market_id)
                continue

            best_ask = fe._normalize_price_to_tick(book_top.best_ask, book_top.tick_size)
            if best_ask <= 0 or best_ask > max_ask:
                skipped += 1
                LOG.warning(
                    "SKIP ask=%.4f hors bande scalping (max=%.4f) market_id=%s",
                    best_ask,
                    max_ask,
                    sig.market_id,
                )
                continue

            effective_p_win = 1.0
            ev = effective_p_win - best_ask
            barbell_cond = "SCALP"

            LOG.info(
                "Scoop candidat market_id=%s ask=%.4f ask_size=%.3f tick=%s ev=%.4f",
                sig.market_id,
                best_ask,
                book_top.best_ask_size,
                f"{book_top.tick_size:.6f}" if book_top.tick_size else "n/a",
                ev,
            )

            eff_budget = (
                min(budget_cfg, remaining_cash) if remaining_cash is not math.inf else budget_cfg
            )
            size_shares = eff_budget / best_ask
            if book_top.best_ask_size > 0:
                size_shares = min(size_shares, book_top.best_ask_size)
            if not dry and remaining_cash is not math.inf:
                max_sh_from_cash = remaining_cash / best_ask
                size_shares = min(size_shares, max_sh_from_cash)
            notional_usdc = size_shares * best_ask
            if notional_usdc + 1e-9 < min_notional:
                skipped += 1
                LOG.warning(
                    "SKIP notionnel %.4f < min %.2f market_id=%s",
                    notional_usdc,
                    min_notional,
                    sig.market_id,
                )
                continue

            if dry:
                qtext = fe._fetch_market_question(sig.market_id) or f"(marché {sig.market_id[:16]}…)"
                LOG.info(
                    "[DRY-RUN RT] LIMIT BUY YES — market_id=%s question=%r "
                    "shares=%.6f price=%.6f notionnel~%.4f USDC",
                    sig.market_id,
                    qtext[:120],
                    size_shares,
                    best_ask,
                    notional_usdc,
                )
                dry_simulated += 1
                continue

            resp = fe._place_limit_buy(token_id=token_id, price=best_ask, size=size_shares)
            ok_post, reject_reason = fe._clob_order_accepted(resp)
            if not ok_post:
                LOG.error(
                    "ORDER RT REJECTED market_id=%s raison=%s réponse=%s",
                    sig.market_id,
                    reject_reason,
                    str(resp)[:1200],
                )
                skipped += 1
                continue

            matched_size, fill_status = fe._extract_fill_info(resp, size_shares)
            if fill_status == "partial":
                LOG.warning(
                    "PARTIAL RT market_id=%s matched=%.3f/%.3f",
                    sig.market_id,
                    matched_size,
                    size_shares,
                )
            elif fill_status == "pending":
                LOG.info("ORDER RT POSTED market_id=%s pending", sig.market_id)

            fe._record_trade(
                conn,
                signal=sig,
                effective_p_win=effective_p_win,
                best_ask=best_ask,
                ev=ev,
                barbell_cond=barbell_cond,
                token_id=token_id,
                order_resp=resp,
                requested_size=size_shares,
                matched_size=matched_size,
            )
            if not dry and remaining_cash is not math.inf:
                remaining_cash = max(0.0, remaining_cash - float(matched_size) * float(best_ask))
            executed += 1
            LOG.info(
                "EXECUTED RT market_id=%s ask=%.4f — enregistré portfolio_rt (OPEN)",
                sig.market_id,
                best_ask,
            )

            stake_usdc = float(matched_size) * float(best_ask)
            oid = str(resp.get("orderID") or resp.get("order_id") or "")
            try:
                alert_rt_scalp_success(
                    station=sig.station,
                    shares=float(matched_size),
                    price=float(best_ask),
                    stake_usdc=stake_usdc,
                    market_id=sig.market_id,
                    order_id=oid,
                    condition_met=True,
                )
            except Exception:
                LOG.debug("Telegram scalp ignorée", exc_info=True)
    finally:
        conn.close()

    LOG.info(
        "finance_rt terminé: executed_live=%s dry_simulated=%s skipped=%s scalp_candidates=%s",
        executed,
        dry_simulated,
        skipped,
        len(scalp_signals),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
