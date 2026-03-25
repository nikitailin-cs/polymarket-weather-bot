#!/usr/bin/env python3
from __future__ import annotations

import inspect
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

# .../POLYMARKET/bots/bot1/execution/status_live.py -> .../POLYMARKET
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402
from py_clob_client.client import ClobClient  # noqa: E402
from py_clob_client.constants import POLYGON  # noqa: E402
from config.settings import resolve_trading_private_key  # noqa: E402
from core.bot1_data_paths import portfolio_sqlite_path  # noqa: E402
from core.polymarket_proxy import apply_proxy_env_for_clob  # noqa: E402

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
CLOB_HOST = "https://clob.polymarket.com"


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.6f}"
    return str(v)


def _load_env() -> tuple[str, str, str]:
    load_dotenv(dotenv_path=_ROOT / ".env", override=False)
    rpc = os.getenv("POLYGON_RPC_URL", "").strip()
    pk = resolve_trading_private_key() or ""
    addr = os.getenv("PUBLIC_ADDRESS", "").strip()
    return rpc, pk, addr


def _get_usdc_onchain_safe(wallet_address: str = "") -> tuple[float, float, float]:
    """
    Import tardif pour garantir que POLYGON_RPC_URL est chargé depuis .env
    avant l'initialisation interne de ctf_manager.
    """
    try:
        from execution.ctf_manager import get_usdc_balances  # noqa: WPS433

        balances = get_usdc_balances(wallet_address or None)
        return (
            float(balances.get("total", 0.0)),
            float(balances.get("bridged", 0.0)),
            float(balances.get("native", 0.0)),
        )
    except Exception:
        return 0.0, 0.0, 0.0


def _build_client() -> Optional[ClobClient]:
    rpc, pk, addr = _load_env()
    apply_proxy_env_for_clob()
    if not pk or not addr:
        print("ENV manquant: PRIVATE_KEY ou POLYMARKET_PKEY / PUBLIC_ADDRESS", flush=True)
        return None
    kwargs: dict[str, Any] = {
        "host": CLOB_HOST,
        "key": pk,
        "chain_id": POLYGON,
        "funder": addr,
        "signature_type": 1,
    }
    params = set(inspect.signature(ClobClient).parameters.keys())
    if "host_rpc" in params and rpc:
        kwargs["host_rpc"] = rpc
    client = ClobClient(**kwargs)

    # Compat credentials API
    if hasattr(client, "create_or_fetch_api_creds"):
        creds = client.create_or_fetch_api_creds()
    elif hasattr(client, "create_or_derive_api_creds"):
        creds = client.create_or_derive_api_creds()
    else:
        creds = None
    if creds is not None:
        if hasattr(client, "set_creds"):
            client.set_creds(creds)
        elif hasattr(client, "set_api_creds"):
            client.set_api_creds(creds)
    return client


def _portfolio_snapshot(limit: int = 10) -> list[tuple]:
    db = portfolio_sqlite_path()
    if not db.is_file():
        return []
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            """
            SELECT traded_at_utc, market_id, recommended_side, best_ask, ev, order_id
            FROM executed_trades
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        conn.close()


def _call_first(client: ClobClient, method_names: list[str]) -> tuple[str, Any]:
    for name in method_names:
        fn = getattr(client, name, None)
        if callable(fn):
            try:
                return name, fn()
            except TypeError:
                # signature differs / requires args
                continue
            except Exception as exc:
                return name, f"ERROR: {exc}"
    return "-", "UNSUPPORTED"


def _fetch_clob_balance(client: ClobClient) -> tuple[str, Any]:
    """
    py_clob_client 0.34 : pas de get_balance() ; get_balance_allowance(None) plante
    (accès à params.signature_type). Il faut BalanceAllowanceParams(COLLATERAL).
    """
    fn = getattr(client, "get_balance_allowance", None)
    if not callable(fn):
        return "-", "UNSUPPORTED"
    try:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams  # type: ignore

        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        return "get_balance_allowance(COLLATERAL)", fn(params)
    except Exception as exc:
        return "get_balance_allowance", f"ERROR: {exc}"


def main() -> int:
    rpc, _, addr = _load_env()
    print("=== POLYMETEO LIVE STATUS ===")
    print("wallet           :", addr or "-")
    print("rpc_configured   :", "yes" if bool(rpc) else "no")
    usdc_total, usdc_bridged, usdc_native = _get_usdc_onchain_safe(addr)
    print("usdc_onchain     :", f"{usdc_total:.4f}")
    print("usdc_bridged     :", f"{usdc_bridged:.4f}")
    print("usdc_native      :", f"{usdc_native:.4f}")

    trades = _portfolio_snapshot(limit=10)
    print("recent_trades_db :", len(trades))
    for t in trades:
        ts, mid, side, ask, ev, oid = t
        print(
            f"  - {ts} {_fmt(mid)[:10]}... side={_fmt(side)} ask={_fmt(ask)} "
            f"ev={_fmt(ev)} order_id={_fmt(oid)}"
        )

    client = _build_client()
    if client is None:
        print("clob_client      : unavailable")
        return 0

    orders_method, orders_data = _call_first(
        client,
        [
            "get_orders",
            "get_open_orders",
            "get_active_orders",
        ],
    )
    print("orders_method    :", orders_method)
    if isinstance(orders_data, (dict, list)):
        try:
            n = len(orders_data) if isinstance(orders_data, list) else len(orders_data.keys())
        except Exception:
            n = -1
        print("orders_payload   :", f"type={type(orders_data).__name__} size={n}")
    else:
        print("orders_payload   :", _fmt(orders_data))

    balance_method, balance_data = _fetch_clob_balance(client)
    print("balance_method   :", balance_method)
    if balance_method == "-" and balance_data == "UNSUPPORTED":
        balance_method, balance_data = _call_first(client, ["get_balance"])
    if balance_method == "-":
        print("balance_payload  : unsupported on this SDK version")
    elif isinstance(balance_data, (dict, list)):
        print("balance_payload  :", json.dumps(balance_data, ensure_ascii=False)[:500])
    else:
        print("balance_payload  :", _fmt(balance_data))

    print("=== END STATUS ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
