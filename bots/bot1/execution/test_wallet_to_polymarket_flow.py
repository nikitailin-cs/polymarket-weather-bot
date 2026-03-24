#!/usr/bin/env python3
from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# .../POLYMARKET/bots/bot1/execution/test_wallet_to_polymarket_flow.py -> .../POLYMARKET
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.settings import FEE_BPS  # noqa: E402
from execution.ctf_manager import get_pol_balance, get_usdc_balances  # noqa: E402

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    def load_dotenv(*args: Any, **kwargs: Any) -> bool:  # type: ignore
        return False


LOG = logging.getLogger("e2e_wallet_to_polymarket")
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
DEFAULT_SIZE_USDC = 1.0
POLYGON_CHAIN_ID = 137


@dataclass
class StepResult:
    name: str
    ok: bool
    details: dict[str, Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _setup_logging(verbose: bool) -> Path:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    log_dir = _ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utc_now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"e2e_payment_flow_{stamp}.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    )
    logging.getLogger().addHandler(file_handler)
    LOG.info("Log file: %s", log_path)
    return log_path


def _http_get_json(url: str, timeout: int = 20) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "e2e-payment-flow/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _extract_token_pair(raw_market: dict[str, Any]) -> Optional[tuple[str, str]]:
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
        yes_idx = norm.index("yes")
        no_idx = norm.index("no")
    except ValueError:
        return None
    yes_token = str(token_ids[yes_idx]).strip()
    no_token = str(token_ids[no_idx]).strip()
    if not yes_token or not no_token:
        return None
    return yes_token, no_token


def _resolve_token_from_market_id(market_id: str, side: str) -> Optional[str]:
    side = side.upper()
    if side not in ("YES", "NO"):
        raise ValueError("side must be YES or NO")

    # Tentative 1: market id explicite
    q1 = urllib.parse.urlencode({"id": market_id, "limit": 1})
    try:
        data = _http_get_json(f"{GAMMA_MARKETS_URL}?{q1}")
        if isinstance(data, list) and data:
            pair = _extract_token_pair(data[0])
            if pair is not None:
                return pair[0] if side == "YES" else pair[1]
    except Exception:
        pass

    # Tentative 2: condition_id (plus fréquent dans ce repo)
    q2 = urllib.parse.urlencode({"condition_ids": market_id, "limit": 1})
    try:
        data = _http_get_json(f"{GAMMA_MARKETS_URL}?{q2}")
        if isinstance(data, list) and data:
            pair = _extract_token_pair(data[0])
            if pair is not None:
                return pair[0] if side == "YES" else pair[1]
    except Exception:
        pass
    return None


def _fetch_best_ask(token_id: str) -> tuple[Optional[float], dict[str, Any]]:
    data = _http_get_json(f"{CLOB_HOST}/book?token_id={urllib.parse.quote(token_id)}")
    if not isinstance(data, dict):
        return None, {"book_type": type(data).__name__}
    asks = data.get("asks")
    if not isinstance(asks, list) or not asks:
        return None, {"book": data}
    best = None
    for row in asks:
        price = 0.0
        if isinstance(row, dict):
            price = _safe_float(row.get("price"), 0.0)
        elif isinstance(row, (list, tuple)) and row:
            price = _safe_float(row[0], 0.0)
        if price <= 0:
            continue
        if best is None or price < best:
            best = price
    return best, {"book": data}


def _build_client() -> Any:
    try:
        from py_clob_client.client import ClobClient  # type: ignore
        from py_clob_client.constants import POLYGON  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "py_clob_client introuvable. Active le bon venv ou installe requirements."
        ) from exc

    load_dotenv(dotenv_path=_ROOT / ".env", override=False)
    private_key = os.getenv("PRIVATE_KEY", "").strip()
    public_address = os.getenv("PUBLIC_ADDRESS", "").strip()
    host_rpc = os.getenv("POLYGON_RPC_URL", "").strip()
    if not private_key:
        raise ValueError("PRIVATE_KEY manquant dans .env")
    if not public_address:
        raise ValueError("PUBLIC_ADDRESS manquant dans .env")

    kwargs: dict[str, Any] = {
        "host": CLOB_HOST,
        "key": private_key,
        "chain_id": POLYGON,
        "funder": public_address,
        "signature_type": 1,
    }
    init_params = set(inspect.signature(ClobClient).parameters.keys())
    if host_rpc and "host_rpc" in init_params:
        kwargs["host_rpc"] = host_rpc
    client = ClobClient(**kwargs)

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


def _try_get_balance(client: Any) -> tuple[str, Any]:
    for method_name in ("get_balance", "get_balance_allowance"):
        fn = getattr(client, method_name, None)
        if callable(fn):
            try:
                return method_name, fn()
            except TypeError as exc:
                return method_name, {"warning": f"signature mismatch: {exc}"}
            except Exception as exc:
                return method_name, {"warning": str(exc)}
    return "-", {"warning": "no balance method available on this SDK"}


def _submit_or_simulate_order(
    client: Any,
    payload: dict[str, Any],
    execute_live: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        signed_or_resp = client.create_order(payload)
    except Exception as first_exc:
        # Compat SDK legacy: create_order peut exiger un objet attributaire.
        order_obj: Any
        try:
            from py_clob_client.clob_types import OrderArgs  # type: ignore

            order_obj = OrderArgs(
                token_id=payload["token_id"],
                price=payload["price"],
                side=payload["side"],
                size=payload["size"],
                fee_rate_bps=payload.get("fee_rate_bps", 0),
            )
        except Exception:
            from types import SimpleNamespace

            order_obj = SimpleNamespace(
                token_id=payload["token_id"],
                price=payload["price"],
                side=payload["side"],
                size=payload["size"],
                fee_rate_bps=payload.get("fee_rate_bps", 0),
            )
        try:
            signed_or_resp = client.create_order(order_obj)
        except Exception as second_exc:
            raise RuntimeError(
                f"create_order a échoué (dict puis objet): {first_exc} | {second_exc}"
            ) from second_exc
    details: dict[str, Any] = {
        "execute_live": execute_live,
        "signed_payload_type": type(signed_or_resp).__name__,
    }

    if not execute_live:
        return (
            {"status": "SIMULATED_ONLY", "order_id": None, "raw": signed_or_resp},
            details,
        )

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
                resp = post_fn(signed_or_resp, order_type=order_type)
            except TypeError:
                resp = post_fn(signed_or_resp, order_type)
        else:
            resp = post_fn(signed_or_resp)
        details["submit_path"] = "post_order"
        return (resp if isinstance(resp, dict) else {"raw_response": resp}, details)

    create_and_post = getattr(client, "create_and_post_order", None)
    if callable(create_and_post):
        resp = create_and_post(payload)
        details["submit_path"] = "create_and_post_order"
        return (resp if isinstance(resp, dict) else {"raw_response": resp}, details)

    details["submit_path"] = "none_available"
    return (
        {"status": "FAILED_NO_POST_METHOD", "order_id": None, "raw": signed_or_resp},
        details,
    )


def run() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnostic E2E wallet -> blockchain -> polymarket with verbose logs."
    )
    parser.add_argument("--token-id", default="", help="Token CLOB à tester")
    parser.add_argument("--market-id", default="", help="Market id (résolution auto token)")
    parser.add_argument("--side", default="YES", choices=["YES", "NO"], help="Côté du token")
    parser.add_argument(
        "--size-usdc",
        type=float,
        default=DEFAULT_SIZE_USDC,
        help="Taille ordre test en USDC",
    )
    parser.add_argument(
        "--execute-live",
        action="store_true",
        help="Poste réellement l'ordre sur CLOB (sinon simulation sign-only).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Active logs DEBUG massifs",
    )
    args = parser.parse_args()

    if args.market_id.strip() in {"<MARKET_ID>", "MARKET_ID", "<market_id>"}:
        print("Erreur: remplace <MARKET_ID> par un vrai market id 0x...", file=sys.stderr)
        return 2

    log_path = _setup_logging(args.verbose)
    started_at = _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    results: list[StepResult] = []

    def step(name: str, ok: bool, **details: Any) -> None:
        results.append(StepResult(name=name, ok=ok, details=details))
        (LOG.info if ok else LOG.error)("STEP %-32s %s", name, "OK" if ok else "FAIL")
        LOG.debug("STEP_DETAILS %s => %s", name, json.dumps(details, default=str)[:2000])

    # 1) ENV
    load_dotenv(dotenv_path=_ROOT / ".env", override=False)
    env_presence = {
        "PRIVATE_KEY": bool(os.getenv("PRIVATE_KEY")),
        "PUBLIC_ADDRESS": bool(os.getenv("PUBLIC_ADDRESS")),
        "POLYGON_RPC_URL": bool(os.getenv("POLYGON_RPC_URL")),
    }
    step("env_variables_present", all(env_presence.values()), **env_presence)
    if not all(env_presence.values()):
        LOG.error("Variables .env incomplètes, arrêt.")
        return 2

    # 2) Wallet balances on-chain
    try:
        usdc = get_usdc_balances()
        pol = get_pol_balance()
        step(
            "wallet_onchain_balances",
            True,
            usdc_total=usdc.get("total", 0.0),
            usdc_bridged=usdc.get("bridged", 0.0),
            usdc_native=usdc.get("native", 0.0),
            pol_balance=pol,
        )
    except Exception as exc:
        step("wallet_onchain_balances", False, error=str(exc))

    # 3) CLOB auth client
    client: Optional[Any] = None
    try:
        client = _build_client()
        step("clob_client_auth", True, host=CLOB_HOST, chain_id=POLYGON_CHAIN_ID)
    except Exception as exc:
        step("clob_client_auth", False, error=str(exc))
        return 3

    # 4) CLOB balance
    method_name, bal = _try_get_balance(client)
    # Etape informative: certains SDK ne supportent pas la lecture balance sans args.
    bal_ok = True
    step("clob_balance_probe", bal_ok, method=method_name, payload=bal)

    # 5) Token resolution + book
    token_id = (args.token_id or "").strip()
    if not token_id and args.market_id.strip():
        try:
            token_id = _resolve_token_from_market_id(args.market_id.strip(), args.side)
            step(
                "resolve_token_from_market",
                bool(token_id),
                market_id=args.market_id.strip(),
                side=args.side,
                token_id=token_id,
            )
        except Exception as exc:
            step(
                "resolve_token_from_market",
                False,
                error=str(exc),
                hint="verifie un vrai market_id 0x... (pas le placeholder)",
            )
    elif token_id:
        step("token_input", True, token_id=token_id, source="cli")
    else:
        step(
            "token_input",
            False,
            error="fournir --token-id ou --market-id",
        )
        return 4

    if not token_id:
        return 4

    best_ask: Optional[float] = None
    try:
        best_ask, book_details = _fetch_best_ask(token_id)
        step(
            "orderbook_fetch",
            best_ask is not None and best_ask > 0,
            token_id=token_id,
            best_ask=best_ask,
            book_excerpt=str(book_details)[:1800],
        )
    except Exception as exc:
        step("orderbook_fetch", False, token_id=token_id, error=str(exc))
        return 5

    if not best_ask or best_ask <= 0:
        LOG.error("Pas de best ask exploitable, arrêt.")
        return 5

    # 6) Build and (optionally) submit test order
    payload = {
        "token_id": token_id,
        "price": best_ask,
        "side": "BUY",
        "size": float(args.size_usdc),
        "fee_rate_bps": FEE_BPS,
    }
    step("build_order_payload", True, payload=payload, execute_live=args.execute_live)

    try:
        submit_resp, submit_details = _submit_or_simulate_order(
            client=client,
            payload=payload,
            execute_live=args.execute_live,
        )
        order_id = (
            submit_resp.get("orderID")
            or submit_resp.get("order_id")
            or submit_resp.get("id")
            or None
        )
        ok = True
        if args.execute_live and order_id is None:
            status = str(submit_resp.get("status", "")).upper()
            ok = status not in ("FAILED", "REJECTED", "ERROR")
        step(
            "submit_order",
            ok,
            order_id=order_id,
            submit_details=submit_details,
            response_excerpt=str(submit_resp)[:2000],
        )
    except Exception as exc:
        step("submit_order", False, error=str(exc))

    ended_at = _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    ok_count = sum(1 for s in results if s.ok)
    final_status = "PASS" if ok_count == len(results) else "PARTIAL_FAIL"

    report = {
        "started_at_utc": started_at,
        "ended_at_utc": ended_at,
        "project_root": str(_ROOT),
        "mode": "LIVE_POST" if args.execute_live else "SIMULATION_ONLY",
        "args": vars(args),
        "summary": {
            "final_status": final_status,
            "ok_steps": ok_count,
            "total_steps": len(results),
        },
        "steps": [asdict(s) for s in results],
        "log_file": str(log_path),
    }
    report_path = _ROOT / "data" / "logs" / f"e2e_payment_flow_report_{_utc_now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    LOG.info("=== TEST FLOW FINISHED ===")
    LOG.info("Status: %s", final_status)
    LOG.info("Report: %s", report_path)
    print(json.dumps(report["summary"], ensure_ascii=False))

    return 0 if final_status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(run())
