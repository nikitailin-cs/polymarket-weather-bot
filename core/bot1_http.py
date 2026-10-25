"""
HTTP GET JSON partagé pour bot1 (Gamma, carnet CLOB, etc.) : même proxy SOCKS que
``finance_engine`` (``POLYMARKET_PROXY_ENABLED`` + ``POLYMARKET_SOCKS5_*``).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env", override=False)
except ImportError:
    pass

import requests  # noqa: E402

from core.polymarket_proxy import build_socks5_proxy_url  # noqa: E402

try:
    DEFAULT_TIMEOUT_SEC = float(
        os.getenv("BOT1_HTTP_TIMEOUT_SEC", os.getenv("GAMMA_HTTP_TIMEOUT_SEC", "20"))
    )
except ValueError:
    DEFAULT_TIMEOUT_SEC = 20.0

DEFAULT_USER_AGENT = os.getenv("BOT1_HTTP_USER_AGENT", "POLYMARKET-bot1-http/1.0")


def bot1_http_get_json(url: str, *, timeout: Optional[float] = None, user_agent: Optional[str] = None) -> Any:
    """
    GET JSON avec proxy SOCKS si configuré (identique à l’usage ``requests`` + ``build_socks5_proxy_url``
    dans ``finance_engine``).
    """
    t = float(timeout if timeout is not None else DEFAULT_TIMEOUT_SEC)
    ua = (user_agent or DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT
    proxy_url = build_socks5_proxy_url()
    # httpx first: on some networks the requests/urllib3 path to Polymarket hosts is answered by an
    # ISP block page (certificate mismatch) while httpx gets through; requests stays as the fallback.
    try:
        import httpx

        kwargs: dict = {"timeout": t, "headers": {"User-Agent": ua}}
        if proxy_url:
            kwargs["proxy"] = proxy_url
        resp_x = httpx.get(url, **kwargs)
        resp_x.raise_for_status()
        return resp_x.json()
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - fall back to requests below
        pass
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    resp = requests.get(url, timeout=t, headers={"User-Agent": ua}, proxies=proxies)
    resp.raise_for_status()
    return resp.json()
