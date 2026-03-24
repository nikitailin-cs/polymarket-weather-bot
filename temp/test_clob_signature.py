#!/usr/bin/env python3
"""
Test diagnostic : même chaîne que finance_engine (funder, signature_type, proxy SOCKS).

  # Signe localement uniquement (aucun ordre sur le carnet)
  python temp/test_clob_signature.py --token-id <CLOB_TOKEN_ID>

  # Envoie un ordre réel au CLOB (petit montant — à tes risques)
  python temp/test_clob_signature.py --token-id <ID> --live --size 1

  # Résoudre le token depuis un condition_id / market id Gamma
  python temp/test_clob_signature.py --market-id 0x... --side YES

Variables .env utiles : PRIVATE_KEY (ou POLYMARKET_PKEY), PUBLIC_ADDRESS ou POLYMARKET_PROXY_ADDRESS,
POLYMARKET_SIGNATURE_TYPE, POLYGON_RPC_URL, POLYMARKET_PROXY_* pour SOCKS.
"""
from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import sys
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
except Exception:

    def load_dotenv(*args: Any, **kwargs: Any) -> bool:  # type: ignore
        return False


from config.settings import FEE_BPS, resolve_trading_private_key  # noqa: E402
from core.client import invoke_clob_post_order, resolve_clob_funder  # noqa: E402
from core.polymarket_proxy import apply_proxy_env_for_clob, build_socks5_proxy_url  # noqa: E402

import requests  # noqa: E402

LOG = logging.getLogger("test_clob_signature")
_PLACEHOLDER_HINT = (
    "Remplace les exemples de la doc par de vraies valeurs : --token-id est un long nombre "
    "(clob token id), ou --market-id avec un condition_id hex complet (64 car. après 0x), "
    "pas la chaîne littérale « <CLOB_TOKEN_ID> » ni « 0x... »."
)


def _docs_placeholder_error(token_or_market: str, which: str) -> Optional[str]:
    s = token_or_market.strip()
    if not s:
        return None
    if "<" in s or ">" in s:
        return f"{which} contient <…> : {_PLACEHOLDER_HINT}"
    low = s.lower()
    if "clob_token_id" in low or s.strip() == "...":
        return f"{which} ressemble à un placeholder : {_PLACEHOLDER_HINT}"
    if which == "market-id" and (s in ("0x...", "0X...") or (s.startswith("0x") and s.endswith("..."))):
        return f"market-id={s!r} est un exemple tronqué : {_PLACEHOLDER_HINT}"
    return None
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _http_get_json(url: str, timeout: int = 25) -> Any:
    proxy_url = build_socks5_proxy_url()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    r = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "test-clob-signature/1.0"},
        proxies=proxies,
    )
    r.raise_for_status()
    return r.json()


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


def resolve_token_id(market_id: str, side: str) -> Optional[str]:
    side = side.upper()
    if side not in ("YES", "NO"):
        raise ValueError("side doit être YES ou NO")
    for params in (
        {"id": market_id, "limit": 1},
        {"condition_ids": market_id, "limit": 1},
    ):
        q = urllib.parse.urlencode(params)
        try:
            data = _http_get_json(f"{GAMMA_MARKETS_URL}?{q}")
            if isinstance(data, list) and data:
                pair = _extract_token_pair(data[0])
                if pair:
                    return pair[0] if side == "YES" else pair[1]
        except Exception:
            continue
    return None


def best_ask_price(token_id: str) -> Optional[float]:
    url = f"{CLOB_HOST}/book?token_id={urllib.parse.quote(token_id)}"
    try:
        data = _http_get_json(url)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if code == 404:
            LOG.warning(
                "CLOB /book 404 pour token_id=%r — id invalide ou carnet inexistant. %s",
                token_id,
                _PLACEHOLDER_HINT,
            )
        else:
            LOG.warning("CLOB /book erreur HTTP %s: %s", code, exc)
        return None
    if not isinstance(data, dict):
        return None
    asks = data.get("asks")
    if not isinstance(asks, list) or not asks:
        return None
    best: Optional[float] = None
    for row in asks:
        p = 0.0
        if isinstance(row, dict):
            p = float(row.get("price") or 0)
        elif isinstance(row, (list, tuple)) and row:
            p = float(row[0] or 0)
        if p <= 0:
            continue
        if best is None or p < best:
            best = p
    return best


def _derived_eoa_address(private_key: str) -> Optional[str]:
    try:
        from eth_account import Account  # type: ignore

        return Account.from_key(private_key).address
    except Exception:
        return None


def build_clob_client() -> Any:
    load_dotenv(dotenv_path=_ROOT / ".env", override=False)
    apply_proxy_env_for_clob()
    from py_clob_client.client import ClobClient  # type: ignore
    from py_clob_client.constants import POLYGON  # type: ignore

    private_key = resolve_trading_private_key()
    if not private_key:
        raise SystemExit("PRIVATE_KEY ou POLYMARKET_PKEY manquant (.env ou environnement)")

    funder = resolve_clob_funder()
    if not funder:
        raise SystemExit(
            "Funder manquant : PUBLIC_ADDRESS, POLYMARKET_PROXY_ADDRESS ou POLYMARKET_FUNDER"
        )

    sig_type_raw = os.getenv("POLYMARKET_SIGNATURE_TYPE", "1").strip()
    try:
        signature_type = int(sig_type_raw)
    except ValueError:
        signature_type = 1

    host_rpc = os.getenv("POLYGON_RPC_URL", "").strip()
    kwargs: dict[str, Any] = {
        "host": CLOB_HOST,
        "key": private_key,
        "chain_id": POLYGON,
        "funder": funder,
        "signature_type": signature_type,
    }
    init_params = set(inspect.signature(ClobClient).parameters.keys())
    if host_rpc and "host_rpc" in init_params:
        kwargs["host_rpc"] = host_rpc
    elif host_rpc:
        LOG.warning("ClobClient sans host_rpc — pip install -U py-clob-client recommandé")

    derived = _derived_eoa_address(private_key)
    LOG.info("signature_type=%s funder=%s", signature_type, funder)
    if derived:
        LOG.info("adresse dérivée de la clé .env (EOA)=%s", derived)
        if derived.lower() != funder.lower():
            LOG.info(
                "funder != EOA de la clé : normal pour un compte proxy Polymarket (signature_type=1)"
            )

    client = ClobClient(**kwargs)
    if hasattr(client, "create_or_fetch_api_creds"):
        creds = client.create_or_fetch_api_creds()
    elif hasattr(client, "create_or_derive_api_creds"):
        creds = client.create_or_derive_api_creds()
    else:
        raise SystemExit("SDK sans create_or_fetch_api_creds")
    if hasattr(client, "set_creds"):
        client.set_creds(creds)
    elif hasattr(client, "set_api_creds"):
        client.set_api_creds(creds)
    return client


def create_signed_order(client: Any, token_id: str, price: float, size: float) -> Any:
    if not (0 < price <= 1):
        raise ValueError(f"prix invalide: {price}")
    if size <= 0:
        raise ValueError("size > 0 requis")
    payload = {
        "token_id": token_id,
        "price": price,
        "side": "BUY",
        "size": size,
        "fee_rate_bps": FEE_BPS,
    }
    try:
        return client.create_order(payload)
    except AttributeError:
        pass
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
        okwargs: dict[str, Any] = {}
        for name, param in sig.parameters.items():
            if name in defaults:
                okwargs[name] = defaults[name]
            elif param.default is not inspect.Parameter.empty:
                okwargs[name] = param.default
            else:
                okwargs[name] = 0
        return client.create_order(OrderArgs(**okwargs))
    except Exception:
        return client.create_order(
            SimpleNamespace(
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
        )


def main() -> int:
    p = argparse.ArgumentParser(
        description="Test signature CLOB Polymarket (dry-run ou --live)",
        epilog=(
            "Exemples (valeurs factices) :\n"
            "  python temp/test_clob_signature.py --token-id 12345678901234567890 --price 0.05\n"
            "  python temp/test_clob_signature.py --market-id 0xabc...condition64hex --side YES --price 0.1\n"
            "Ne pas copier-coller <CLOB_TOKEN_ID> ni 0x... depuis la doc."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--token-id", default="", help="Token CLOB (outcome)")
    p.add_argument("--market-id", default="", help="condition_id ou id Gamma pour résoudre le token")
    p.add_argument("--side", default="YES", choices=["YES", "NO"])
    p.add_argument("--price", type=float, default=0.0, help="Prix limite 0–1 (défaut: best ask)")
    p.add_argument("--size", type=float, default=1.0, help="Taille ordre (USDC / notional selon SDK)")
    p.add_argument(
        "--live",
        action="store_true",
        help="Appelle post_order (ordre réel sur le carnet). Sans ce flag : create_order seulement.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    token_id = args.token_id.strip()
    if not token_id:
        mid = args.market_id.strip()
        if not mid:
            p.error("Fournis --token-id ou --market-id")
        ph = _docs_placeholder_error(mid, "market-id")
        if ph:
            p.error(ph)
        token_id = resolve_token_id(mid, args.side) or ""
        if not token_id:
            p.error(
                f"Impossible de résoudre le token pour market-id={mid!r} side={args.side}. "
                "Vérifie que l’id est un condition_id Polymarket (hex) connu de l’API Gamma."
            )
        LOG.info("token_id résolu=%s", token_id)
    else:
        ph = _docs_placeholder_error(token_id, "--token-id")
        if ph:
            p.error(ph)

    price = float(args.price)
    if price <= 0:
        ba = best_ask_price(token_id)
        if ba is None or ba <= 0:
            p.error(
                "Pas de best ask (souvent 404 = mauvais token_id). "
                "Fournis --price entre 0 et 1 (ex. 0.5) ou un --token-id valide. "
                + _PLACEHOLDER_HINT
            )
        price = ba
        LOG.info("prix = best ask = %s", price)

    client = build_clob_client()
    LOG.info("create_order (signe localement) token_id=%s price=%s size=%s", token_id, price, args.size)
    signed = create_signed_order(client, token_id, price, args.size)
    LOG.info("create_order OK type=%s", type(signed).__name__)

    if isinstance(signed, dict):
        keys = list(signed.keys())[:20]
        LOG.info("champs (extrait): %s", keys)

    if not args.live:
        LOG.info(
            "Mode dry-run : aucun post_order. Si la signature locale échoue, une exception "
            "aurait été levée ci-dessus. Pour tester le CLOB : ajoute --live"
        )
        return 0

    try:
        from py_clob_client.clob_types import OrderType  # type: ignore

        ot = getattr(OrderType, "GTC", None)
    except Exception:
        ot = None

    LOG.warning("POST ordre réel au CLOB (GTC)")
    try:
        resp = invoke_clob_post_order(client, signed, ot)
    except Exception as exc:
        LOG.error("post_order a échoué: %s", exc)
        return 1

    if isinstance(resp, dict):
        LOG.info("réponse: %s", json.dumps(resp, default=str)[:2000])
        oid = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        if oid:
            LOG.info("ordre accepté id=%s", oid)
            return 0
    LOG.error("réponse inattendue ou sans order id: %r", resp)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
