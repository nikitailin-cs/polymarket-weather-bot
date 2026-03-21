from __future__ import annotations
from typing import Optional
from py_clob_client.client import ClobClient
from config.settings import CLOB_HOST, CHAIN_ID, PRIVATE_KEY

_client: Optional[ClobClient] = None

def get_client() -> ClobClient:
    global _client
    if _client is None:
        _client = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
    return _client

def reset_client() -> None:
    global _client
    _client = None