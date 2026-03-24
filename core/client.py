from __future__ import annotations

import inspect
import os
from typing import Any, Optional

from py_clob_client.client import ClobClient

from config.settings import CHAIN_ID, CLOB_HOST, PRIVATE_KEY
from core.polymarket_proxy import apply_proxy_env_for_clob

_client: Optional[ClobClient] = None

def get_client() -> ClobClient:
    global _client
    if _client is None:
        # Active le proxy réseau CLOB (SOCKS5) si configuré.
        apply_proxy_env_for_clob()
        if not PRIVATE_KEY:
            raise ValueError("PRIVATE_KEY manquant (impossible d'initialiser le client CLOB)")

        public_address = os.getenv("PUBLIC_ADDRESS")
        host_rpc = os.getenv("POLYGON_RPC_URL")
        kwargs: dict[str, Any] = {
            "host": CLOB_HOST,
            "key": PRIVATE_KEY,
            "chain_id": CHAIN_ID,
            "signature_type": 1,
        }
        if public_address:
            kwargs["funder"] = public_address

        init_params = set(inspect.signature(ClobClient).parameters.keys())
        if host_rpc and "host_rpc" in init_params:
            kwargs["host_rpc"] = host_rpc

        _client = ClobClient(**kwargs)

        # Auth API CLOB (nécessaire pour poster/consulter des ordres selon versions SDK).
        if hasattr(_client, "create_or_fetch_api_creds"):
            creds = _client.create_or_fetch_api_creds()
        elif hasattr(_client, "create_or_derive_api_creds"):
            creds = _client.create_or_derive_api_creds()
        else:
            creds = None

        if creds is not None:
            if hasattr(_client, "set_creds"):
                _client.set_creds(creds)
            elif hasattr(_client, "set_api_creds"):
                _client.set_api_creds(creds)
    return _client

def reset_client() -> None:
    global _client
    _client = None