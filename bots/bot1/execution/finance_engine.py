#!/usr/bin/env python3
from __future__ import annotations

import json
import inspect
import logging
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

# .../POLYMARKET/bots/bot1/execution/finance_engine.py -> .../POLYMARKET
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402
from py_clob_client.client import ClobClient  # noqa: E402
from py_clob_client.constants import POLYGON  # noqa: E402

from config.settings import FEE_BPS  # noqa: E402


LOG = logging.getLogger("finance_engine")
GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_URL = "https://clob.polymarket.com"
ORDER_TIMEOUT_SEC = 20
_clob_client: Optional[ClobClient] = None
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True)
class TradingSignal:
    market_id: str
    station: str
    p_win: float
    delta: float
    recommended_side: str
    forecast_cycle_utc: str


@dataclass(frozen=True)
class TokenPair:
    yes_token_id: str
    no_token_id: str


@dataclass(frozen=True)
class BookTop:
    best_ask: float
    best_ask_size: float
    tick_size: Optional[float]


def _signals_path() -> Path:
    return _ROOT / "data" / "trading_signals.json"


def _portfolio_db_path() -> Path:
    p = _ROOT / "data" / "portfolio.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _today_utc() -> str:
    return _utc_now().strftime("%Y-%m-%d")


def _http_get_json(url: str, timeout: int = ORDER_TIMEOUT_SEC) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "finance-engine/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _get_clob_client() -> ClobClient:
    global _clob_client
    if _clob_client is not None:
        return _clob_client

    load_dotenv(dotenv_path=_ROOT / ".env", override=False)
    poly_rpc_url = os.getenv("POLYGON_RPC_URL")
    if not poly_rpc_url:
        raise ValueError("POLYGON_RPC_URL manquant dans le .env")

    private_key = os.getenv("PRIVATE_KEY")
    public_address = os.getenv("PUBLIC_ADDRESS")
    if not private_key:
        raise ValueError("PRIVATE_KEY manquant dans le .env")
    if not public_address:
        raise ValueError("PUBLIC_ADDRESS manquant dans le .env")

    base_kwargs: dict[str, Any] = {
        "host": CLOB_URL,
        "key": private_key,
        "chain_id": POLYGON,
        "funder": public_address,
        "signature_type": 1,  # EIP-712
    }
    # Compatibilité multi-versions py_clob_client:
    # certaines versions n'acceptent pas host_rpc dans __init__.
    init_params = set(inspect.signature(ClobClient).parameters.keys())
    if "host_rpc" in init_params:
        base_kwargs["host_rpc"] = poly_rpc_url
    else:
        LOG.warning(
            "ClobClient sans support host_rpc détecté; "
            "mise à jour du package recommandée pour RPC privé."
        )

    client = ClobClient(**base_kwargs)
    if hasattr(client, "create_or_fetch_api_creds"):
        creds = client.create_or_fetch_api_creds()
    elif hasattr(client, "create_or_derive_api_creds"):
        creds = client.create_or_derive_api_creds()
    else:
        raise AttributeError(
            "ClobClient ne supporte ni create_or_fetch_api_creds ni create_or_derive_api_creds"
        )
    if hasattr(client, "set_creds"):
        client.set_creds(creds)
    elif hasattr(client, "set_api_creds"):
        client.set_api_creds(creds)
    else:
        raise AttributeError("ClobClient ne supporte ni set_creds ni set_api_creds")
    _clob_client = client
    return client


def _load_signals(path: Path) -> list[TradingSignal]:
    if not path.is_file():
        raise FileNotFoundError(f"Fichier signaux introuvable: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload_cycle = str(payload.get("forecast_cycle_utc", "")).strip()
    raw_signals = payload.get("signals")
    if not isinstance(raw_signals, list):
        raise ValueError("Format invalide: clé 'signals' absente ou non liste")
    out: list[TradingSignal] = []
    for i, row in enumerate(raw_signals):
        if not isinstance(row, dict):
            LOG.warning("Signal[%s] ignoré (format invalide)", i)
            continue
        market_id = str(row.get("market_id", "")).strip()
        if not market_id:
            LOG.warning("Signal[%s] ignoré (market_id vide)", i)
            continue
        side = str(row.get("recommended_side", "YES")).strip().upper()
        if side not in ("YES", "NO"):
            LOG.warning("Signal[%s] ignoré (recommended_side invalide=%r)", i, side)
            continue
        out.append(
            TradingSignal(
                market_id=market_id,
                station=str(row.get("station", "")).strip(),
                p_win=_safe_float(row.get("p_win"), 0.0),
                delta=_safe_float(row.get("delta"), 0.0),
                recommended_side=side,
                forecast_cycle_utc=str(row.get("forecast_cycle_utc") or payload_cycle).strip()
                or "unknown_cycle",
            )
        )
    return out


def _open_portfolio_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
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
    if "forecast_cycle_utc" not in col_names:
        conn.execute(
            "ALTER TABLE executed_trades ADD COLUMN forecast_cycle_utc TEXT NOT NULL DEFAULT ''"
        )
    if "status" not in col_names:
        conn.execute(
            "ALTER TABLE executed_trades ADD COLUMN status TEXT NOT NULL DEFAULT 'OPEN'"
        )
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
        SET forecast_cycle_utc = COALESCE(NULLIF(forecast_cycle_utc, ''), traded_date_utc || 'T00:00:00Z')
        WHERE forecast_cycle_utc IS NULL OR forecast_cycle_utc = ''
        """
    )
    conn.execute(
        """
        UPDATE executed_trades
        SET status = 'OPEN'
        WHERE status IS NULL OR TRIM(status) = ''
        """
    )
    conn.execute("DROP INDEX IF EXISTS ux_executed_trades_market_day")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_executed_trades_market_cycle
        ON executed_trades (market_id, forecast_cycle_utc)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_executed_trades_status
        ON executed_trades (status)
        """
    )
    conn.commit()
    return conn


def _already_traded_for_cycle(conn: sqlite3.Connection, market_id: str, forecast_cycle_utc: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM executed_trades
        WHERE market_id = ? AND forecast_cycle_utc = ?
        LIMIT 1
        """,
        (market_id, forecast_cycle_utc),
    ).fetchone()
    return row is not None


def _extract_token_pair_from_market(raw_market: dict[str, Any]) -> Optional[TokenPair]:
    outcomes = raw_market.get("outcomes")
    token_ids = raw_market.get("clobTokenIds")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = []
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except Exception:
            token_ids = []
    if not isinstance(outcomes, list) or not isinstance(token_ids, list):
        return None
    if len(outcomes) < 2 or len(token_ids) < 2:
        return None
    norm = [str(x).strip().lower() for x in outcomes]
    try:
        yes_i = norm.index("yes")
        no_i = norm.index("no")
    except ValueError:
        return None
    yes_token = str(token_ids[yes_i]).strip()
    no_token = str(token_ids[no_i]).strip()
    if not yes_token or not no_token:
        return None
    return TokenPair(yes_token_id=yes_token, no_token_id=no_token)


def _resolve_tokens_for_market(market_id: str) -> Optional[TokenPair]:
    # Tentative 1: market id exact
    q1 = urllib.parse.urlencode({"id": market_id, "limit": 1})
    u1 = f"{GAMMA_URL}?{q1}"
    try:
        data = _http_get_json(u1)
        if isinstance(data, list) and data:
            pair = _extract_token_pair_from_market(data[0])
            if pair:
                return pair
    except Exception:
        pass

    # Tentative 2: condition id exact
    q2 = urllib.parse.urlencode({"condition_ids": market_id, "limit": 1})
    u2 = f"{GAMMA_URL}?{q2}"
    try:
        data = _http_get_json(u2)
        if isinstance(data, list) and data:
            pair = _extract_token_pair_from_market(data[0])
            if pair:
                return pair
    except Exception:
        pass
    return None


def _extract_best_ask_from_book(book: dict[str, Any]) -> Optional[BookTop]:
    asks = book.get("asks") or book.get("sell") or book.get("ask")
    if not isinstance(asks, list) or not asks:
        return None
    top_price: Optional[float] = None
    top_size: float = 0.0
    for level in asks:
        price = 0.0
        size = 0.0
        if isinstance(level, dict):
            price = _safe_float(level.get("price"), 0.0)
            size = _safe_float(level.get("size"), 0.0)
        elif isinstance(level, (list, tuple)) and level:
            price = _safe_float(level[0], 0.0)
            if len(level) > 1:
                size = _safe_float(level[1], 0.0)
        if price <= 0:
            continue
        if top_price is None or price < top_price:
            top_price = price
            top_size = max(0.0, size)
    if top_price is None:
        return None
    tick_size = _safe_float(book.get("tick_size") or book.get("tickSize"), 0.0)
    return BookTop(
        best_ask=top_price,
        best_ask_size=top_size,
        tick_size=tick_size if tick_size > 0 else None,
    )


def _fetch_book_top(token_id: str) -> Optional[BookTop]:
    # Endpoint public CLOB le plus courant.
    url = f"{CLOB_URL}/book?token_id={urllib.parse.quote(token_id)}"
    data = _http_get_json(url)
    if not isinstance(data, dict):
        return None
    return _extract_best_ask_from_book(data)


def _normalize_price_to_tick(price: float, tick_size: Optional[float]) -> float:
    """
    Aligne le prix limite sur la grille tickSize du marché.
    On arrondit vers le bas pour éviter de payer au-dessus du best ask validé.
    """
    if tick_size is None or tick_size <= 0:
        return price
    try:
        p = Decimal(str(price))
        tick = Decimal(str(tick_size))
        steps = (p / tick).to_integral_value(rounding=ROUND_DOWN)
        normalized = steps * tick
        return float(normalized)
    except (InvalidOperation, ValueError):
        return price


def _barbell_condition(p_win_side: float, best_ask: float) -> Optional[str]:
    # Condition A: steamroller/sécurité
    if p_win_side > 0.90 and best_ask < (p_win_side - 0.05):
        return "A"
    # Condition B: sniper asymétrique/loto
    if p_win_side > 0.15 and best_ask <= 0.05:
        return "B"
    return None


def _place_limit_buy(token_id: str, price: float, size: float) -> dict[str, Any]:
    if size <= 0:
        raise ValueError("size doit être > 0")
    if not (0 < price <= 1):
        raise ValueError(f"price invalide pour CLOB: {price}")
    client = _get_clob_client()
    # Ordre passif uniquement: LIMIT buy au best ask validé.
    order_payload = {
        "token_id": token_id,
        "price": price,
        "side": "BUY",
        "size": size,
        "fee_rate_bps": FEE_BPS,
    }
    try:
        # Certaines versions py_clob_client acceptent un dict.
        signed_or_resp = client.create_order(order_payload)
    except AttributeError:
        # Compat versions legacy: create_order attend un objet avec attributs.
        # On préfère le type OrderArgs du SDK quand disponible.
        order_obj: Any
        try:
            from py_clob_client.clob_types import OrderArgs  # type: ignore

            sig = inspect.signature(OrderArgs)
            defaults: dict[str, Any] = {
                "token_id": token_id,
                "price": price,
                "side": "BUY",
                "size": size,
                "fee_rate_bps": FEE_BPS,
                "taker": ZERO_ADDRESS,
                "maker": ZERO_ADDRESS,
                "nonce": 0,
                "expiration": 0,
                "salt": 0,
            }
            kwargs: dict[str, Any] = {}
            for name, param in sig.parameters.items():
                if name in defaults:
                    kwargs[name] = defaults[name]
                elif param.default is not inspect.Parameter.empty:
                    kwargs[name] = param.default
                else:
                    # Valeur neutre pour champ requis inconnu.
                    kwargs[name] = 0
            order_obj = OrderArgs(**kwargs)
        except Exception:
            # Dernier fallback: namespace attributaire large.
            order_obj = SimpleNamespace(
                token_id=token_id,
                price=price,
                side="BUY",
                size=size,
                fee_rate_bps=FEE_BPS,
                taker=ZERO_ADDRESS,
                maker=ZERO_ADDRESS,
                nonce=0,
                expiration=0,
                salt=0,
            )
        signed_or_resp = client.create_order(order_obj)

    # create_order signe l'ordre; il faut ensuite le poster pour exécution réelle.
    post_fn = getattr(client, "post_order", None)
    if callable(post_fn):
        order_type = None
        try:
            from py_clob_client.clob_types import OrderType  # type: ignore

            order_type = getattr(OrderType, "GTC", None)
        except Exception:
            order_type = None

        try:
            if order_type is not None:
                try:
                    resp = post_fn(signed_or_resp, order_type=order_type)
                except TypeError:
                    resp = post_fn(signed_or_resp, order_type)
            else:
                resp = post_fn(signed_or_resp)
        except Exception:
            resp = signed_or_resp
    else:
        create_and_post = getattr(client, "create_and_post_order", None)
        if callable(create_and_post):
            try:
                resp = create_and_post(order_payload)
            except Exception:
                resp = signed_or_resp
        else:
            resp = signed_or_resp

    if not isinstance(resp, dict):
        return {"raw_response": resp}
    return resp


def _extract_fill_info(order_resp: dict[str, Any], requested_size: float) -> tuple[float, str]:
    size_matched = _safe_float(
        order_resp.get("sizeMatched")
        or order_resp.get("size_matched")
        or order_resp.get("makingAmount")
        or 0.0,
        0.0,
    )
    status = str(order_resp.get("status") or "").lower()
    if size_matched <= 0 and status in ("matched", "filled"):
        size_matched = requested_size
    if size_matched <= 0:
        return 0.0, "pending"
    if size_matched + 1e-9 < requested_size:
        return size_matched, "partial"
    return size_matched, "filled"


def _record_trade(
    conn: sqlite3.Connection,
    *,
    signal: TradingSignal,
    effective_p_win: float,
    best_ask: float,
    ev: float,
    barbell_cond: str,
    token_id: str,
    order_resp: dict[str, Any],
    requested_size: float,
    matched_size: float,
) -> None:
    now = _utc_now()
    order_id = order_resp.get("orderID") or order_resp.get("order_id")
    try:
        raw_response_json = json.dumps(order_resp, ensure_ascii=False, default=str)
    except Exception:
        raw_response_json = json.dumps({"raw_response": str(order_resp)}, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO executed_trades (
            traded_at_utc,
            traded_date_utc,
            forecast_cycle_utc,
            requested_size,
            matched_size,
            spent_usdc,
            market_id,
            station,
            recommended_side,
            p_win,
            effective_p_win,
            delta,
            best_ask,
            ev,
            barbell_condition,
            token_id,
            order_id,
            raw_response_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            now.strftime("%Y-%m-%d"),
            signal.forecast_cycle_utc,
            requested_size,
            matched_size,
            float(matched_size) * float(best_ask),
            signal.market_id,
            signal.station,
            signal.recommended_side,
            signal.p_win,
            effective_p_win,
            signal.delta,
            best_ask,
            ev,
            barbell_cond,
            token_id,
            str(order_id) if order_id is not None else None,
            raw_response_json,
        ),
    )
    conn.commit()


def run(order_size_usdc: float = 2.5) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    # Réduction du bruit HTTP: on garde uniquement warnings/erreurs transport.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    LOG.info("Démarrage du moteur financier - mode silencieux HTTP activé")
    signals = _load_signals(_signals_path())
    if not signals:
        LOG.info("Aucun signal disponible.")
        return 0

    conn = _open_portfolio_db(_portfolio_db_path())
    executed = 0
    skipped = 0
    try:
        for sig in signals:
            if _already_traded_for_cycle(conn, sig.market_id, sig.forecast_cycle_utc):
                skipped += 1
                LOG.info(
                    "SKIP déjà tradé sur ce cycle market_id=%s cycle=%s",
                    sig.market_id,
                    sig.forecast_cycle_utc,
                )
                continue

            tokens = _resolve_tokens_for_market(sig.market_id)
            if tokens is None:
                skipped += 1
                LOG.warning("SKIP token_ids introuvables market_id=%s", sig.market_id)
                continue

            token_id = tokens.yes_token_id if sig.recommended_side == "YES" else tokens.no_token_id
            book_top = _fetch_book_top(token_id)
            if book_top is None:
                skipped += 1
                LOG.warning(
                    "SKIP best ask indisponible market_id=%s side=%s",
                    sig.market_id,
                    sig.recommended_side,
                )
                continue
            best_ask = _normalize_price_to_tick(book_top.best_ask, book_top.tick_size)
            if best_ask <= 0:
                skipped += 1
                LOG.warning(
                    "SKIP prix invalide après tick normalization market_id=%s side=%s",
                    sig.market_id,
                    sig.recommended_side,
                )
                continue

            effective_p_win = sig.p_win if sig.recommended_side == "YES" else (1.0 - sig.p_win)
            ev = effective_p_win - best_ask
            cond = _barbell_condition(effective_p_win, best_ask)

            LOG.info(
                "Signal market_id=%s side=%s p=%.3f ask=%.3f ask_size=%.3f tick=%s ev=%.3f cond=%s",
                sig.market_id,
                sig.recommended_side,
                effective_p_win,
                best_ask,
                book_top.best_ask_size,
                f"{book_top.tick_size:.6f}" if book_top.tick_size else "n/a",
                ev,
                cond or "-",
            )

            if cond is None:
                skipped += 1
                continue

            resp = _place_limit_buy(token_id=token_id, price=best_ask, size=order_size_usdc)
            matched_size, fill_status = _extract_fill_info(resp, order_size_usdc)
            if fill_status == "partial":
                LOG.warning(
                    "PARTIAL FILL market_id=%s side=%s matched=%.3f/%.3f at %.3f",
                    sig.market_id,
                    sig.recommended_side,
                    matched_size,
                    order_size_usdc,
                    best_ask,
                )
            elif fill_status == "pending":
                LOG.info(
                    "ORDER POSTED market_id=%s side=%s pending in book at %.3f",
                    sig.market_id,
                    sig.recommended_side,
                    best_ask,
                )
            _record_trade(
                conn,
                signal=sig,
                effective_p_win=effective_p_win,
                best_ask=best_ask,
                ev=ev,
                barbell_cond=cond,
                token_id=token_id,
                order_resp=resp,
                requested_size=order_size_usdc,
                matched_size=matched_size,
            )
            executed += 1
            LOG.info(
                "EXECUTED market_id=%s side=%s token=%s ask=%.3f",
                sig.market_id,
                sig.recommended_side,
                token_id[:18] + "...",
                best_ask,
            )
    finally:
        conn.close()

    LOG.info("Terminé: executed=%s skipped=%s total=%s", executed, skipped, len(signals))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
