from __future__ import annotations

import os
import urllib.parse
from typing import Optional


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


def apply_proxy_env_for_clob() -> Optional[str]:
    """
    Applique les variables proxy au runtime pour les libs HTTP qui lisent l'env.
    """
    proxy_url = build_socks5_proxy_url()
    if not proxy_url:
        return None

    os.environ["ALL_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["HTTP_PROXY"] = proxy_url
    return proxy_url
