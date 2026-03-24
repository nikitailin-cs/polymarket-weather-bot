from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Optional

LOG = logging.getLogger(__name__)

# Dernière URL pour laquelle le client httpx de py_clob_client a été remplacé.
_last_clob_proxy_url: Optional[str] = None


def _is_enabled() -> bool:
    return os.getenv("POLYMARKET_PROXY_ENABLED", "0").strip() == "1"


def build_socks5_proxy_url() -> Optional[str]:
    """
    Construit l'URL proxy SOCKS5 depuis les variables d'environnement.
    """
    if not _is_enabled():
        return None

    host = os.getenv("POLYMARKET_SOCKS5_HOST", "").strip()
    port = os.getenv("POLYMARKET_SOCKS5_PORT", "").strip()
    user = os.getenv("POLYMARKET_SOCKS5_USERNAME", "").strip()
    password = os.getenv("POLYMARKET_SOCKS5_PASSWORD", "").strip()

    if not host or not port:
        return None

    auth = ""
    if user:
        q_user = urllib.parse.quote(user, safe="")
        q_pass = urllib.parse.quote(password, safe="")
        auth = f"{q_user}:{q_pass}@"
    return f"socks5h://{auth}{host}:{port}"


def patch_py_clob_httpx_client(proxy_url: str) -> None:
    """
    Remplace le httpx.Client module-level de py_clob_client par un client qui
    route explicitement tout le trafic CLOB vers proxy_url.

    py_clob_client définit ``httpx.Client(http2=True)`` sans proxy ; s'appuyer
    uniquement sur HTTP_PROXY/ALL_PROXY ne garantit pas le SOCKS5 (httpx/httpcore
    exigent ``socksio`` pour SOCKS, et l'ordre d'import peut précéder ``os.environ``).
    """
    global _last_clob_proxy_url
    if proxy_url == _last_clob_proxy_url:
        return

    try:
        import socksio  # noqa: F401 — requis par httpcore pour SOCKS5
    except ImportError as e:
        raise RuntimeError(
            "Proxy SOCKS5 activé mais le paquet 'socksio' est absent "
            "(nécessaire à httpx pour SOCKS5). Installez-le : pip install socksio"
        ) from e

    import httpx

    try:
        import py_clob_client.http_helpers.helpers as clob_http
    except ImportError:
        LOG.debug("patch_py_clob_httpx_client: py_clob_client non importé, ignoré")
        return

    old = getattr(clob_http, "_http_client", None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass

    clob_http._http_client = httpx.Client(
        http2=True,
        proxy=proxy_url,
        trust_env=False,
    )
    _last_clob_proxy_url = proxy_url
    LOG.info("Client httpx py_clob_client configuré pour SOCKS5 (proxy explicite)")


def apply_proxy_env_for_clob() -> Optional[str]:
    """
    Applique les variables proxy au runtime pour les libs HTTP qui lisent l'env,
    et force le client httpx interne à py_clob_client à utiliser le même SOCKS5.
    """
    proxy_url = build_socks5_proxy_url()
    if not proxy_url:
        return None

    os.environ["ALL_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["HTTP_PROXY"] = proxy_url
    patch_py_clob_httpx_client(proxy_url)
    return proxy_url
