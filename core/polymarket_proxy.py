from __future__ import annotations

import logging
import os
import urllib.parse
from pathlib import Path
from typing import Any, Optional

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


def describe_socks_proxy_env() -> dict[str, Any]:
    """
    Résumé sans secret pour logs (diagnostic déploiement).
    """
    host = os.getenv("POLYMARKET_SOCKS5_HOST", "").strip()
    port = os.getenv("POLYMARKET_SOCKS5_PORT", "").strip()
    eff = build_socks5_proxy_url()
    return {
        "POLYMARKET_PROXY_ENABLED": _is_enabled(),
        "host_set": bool(host),
        "port_set": bool(port),
        "clob_routes_via_socks": eff is not None,
    }


def _probe_public_egress_ip(
    proxies: Optional[dict[str, str]],
    *,
    timeout: float = 12.0,
) -> tuple[Optional[str], Optional[str]]:
    """
    Retourne (ip, erreur_courte). Utilise requests + api.ipify.org / ifconfig.me.
    """
    try:
        import requests
    except ImportError:
        return None, "requests_non_importable"

    urls = ("https://api.ipify.org", "https://ifconfig.me/ip")
    last_err: Optional[str] = None
    for url in urls:
        try:
            r = requests.get(
                url,
                timeout=timeout,
                proxies=proxies,
                headers={"User-Agent": "polymarket-proxy-audit/1.1"},
            )
            if r.ok:
                ip = (r.text or "").strip().split()[0][:64]
                if ip:
                    return ip, None
        except Exception as exc:
            last_err = str(exc)[:200]
            continue
    return None, last_err or "probe_failed"


def audit_polymarket_network(
    project_root: Path,
    logger: logging.Logger,
    *,
    run_egress_probes: bool,
) -> dict[str, Any]:
    """
    Diagnostic déploiement : .env, flags SOCKS, comparaison IP directe vs SOCKS.
    Aucun mot de passe ni URL complète n'est journalisé.
    """
    env_path = project_root / ".env"
    out: dict[str, Any] = {
        "cwd": os.getcwd(),
        "dotenv_path": str(env_path.resolve()),
        "dotenv_exists": env_path.is_file(),
    }
    if env_path.is_file():
        try:
            out["dotenv_size_bytes"] = env_path.stat().st_size
        except OSError:
            pass

    raw_flag = os.getenv("POLYMARKET_PROXY_ENABLED")
    out["POLYMARKET_PROXY_ENABLED_env"] = raw_flag if raw_flag is not None else "__UNSET__"

    host = os.getenv("POLYMARKET_SOCKS5_HOST", "").strip()
    port = os.getenv("POLYMARKET_SOCKS5_PORT", "").strip()
    user_set = bool(os.getenv("POLYMARKET_SOCKS5_USERNAME", "").strip())
    out["socks_host"] = host if host else None
    out["socks_port"] = port if port else None
    out["socks_user_set"] = user_set

    desc = describe_socks_proxy_env()
    out["describe"] = desc
    socks_eff = bool(desc.get("clob_routes_via_socks"))

    warnings: list[str] = []
    if not env_path.is_file():
        warnings.append("Fichier .env introuvable à project_root — les clés POLYMARKET_* ne sont pas chargées depuis le fichier.")
    if raw_flag is None:
        warnings.append("POLYMARKET_PROXY_ENABLED absent — définis POLYMARKET_PROXY_ENABLED=1 sur le serveur pour le SOCKS.")
    elif str(raw_flag).strip() != "1":
        warnings.append("POLYMARKET_PROXY_ENABLED != 1 — le trafic Polymarket (httpx/requests) sort en DIRECT.")
    if not socks_eff:
        warnings.append(
            "SOCKS inactif ou incomplet: CLOB + gamma-api passent par l'IP du serveur (urllib3 'Starting new HTTPS connection' vers *.polymarket.com = direct)."
        )

    logger.info("[proxy-audit] dotenv_exists=%s path=%s", out["dotenv_exists"], out["dotenv_path"])
    logger.info("[proxy-audit] clob_routes_via_socks=%s host=%s port=%s", socks_eff, out["socks_host"], out["socks_port"])
    logger.info(
        "[proxy-audit] Si SOCKS est vraiment actif: les logs httpcore devraient montrer une connexion TCP "
        "vers l'hôte SOCKS (pas une première socket TLS directe vers clob.polymarket.com:443)."
    )

    for w in warnings:
        logger.warning("[proxy-audit] %s", w)

    out["warnings"] = warnings

    if run_egress_probes:
        ip_d, err_d = _probe_public_egress_ip(None)
        out["egress_ip_direct"] = ip_d
        if err_d:
            out["egress_ip_direct_error"] = err_d
        logger.info("[proxy-audit] IP publique sans proxy (sonde): %s", ip_d or err_d or "?")

        pu = build_socks5_proxy_url()
        if pu:
            ip_p, err_p = _probe_public_egress_ip({"http": pu, "https": pu})
            out["egress_ip_via_socks_probe"] = ip_p
            if err_p:
                out["egress_ip_socks_error"] = err_p
            logger.info("[proxy-audit] IP publique via SOCKS (sonde requests): %s", ip_p or err_p or "?")
            if ip_d and ip_p and ip_d == ip_p:
                msg = (
                    "IP directe et IP via SOCKS identiques — soit le proxy ne change pas la sortie, "
                    "soit le SOCKS n'est pas utilisé par requests (vérifier PySocks, URL, auth)."
                )
                out["socks_probe_same_as_direct"] = True
                warnings.append(msg)
                logger.warning("[proxy-audit] %s", msg)
            elif ip_d and ip_p and ip_d != ip_p:
                out["socks_probe_same_as_direct"] = False
                logger.info("[proxy-audit] Les IP directe et SOCKS diffèrent — le SOCKS modifie bien la sortie (sonde HTTP).")
        else:
            out["egress_ip_via_socks_probe"] = None
            logger.debug("[proxy-audit] Pas de SOCKS effectif — pas de sonde IP via proxy.")

    out["warnings"] = warnings
    return out


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
