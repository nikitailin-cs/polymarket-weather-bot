"""
WebSocket feed temps réel — prix CLOB Polymarket.
Tourne en thread background et met à jour un cache de prix partagé.
Le scanner REST reste en fallback si le WS tombe.
"""
from __future__ import annotations
import json, threading, time, logging
from typing import Dict, Optional
try:
    import websocket
except ImportError:
    websocket = None

log = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Cache partagé : token_id → prix float
_price_cache: Dict[str, float] = {}
_cache_lock  = threading.Lock()
_subscribed  : set = set()
_ws_instance = None
_ws_running  = False


def get_price(token_id: str) -> Optional[float]:
    with _cache_lock:
        return _price_cache.get(token_id)


def update_price(token_id: str, price: float) -> None:
    with _cache_lock:
        _price_cache[token_id] = price


def subscribe_tokens(token_ids: list[str]) -> None:
    """Abonne le WS à une liste de token_ids."""
    global _ws_instance, _subscribed
    new_tokens = [t for t in token_ids if t and t not in _subscribed]
    if not new_tokens or not _ws_instance:
        return
    msg = json.dumps({
        "type":    "subscribe",
        "channel": "price_change",
        "assets_ids": new_tokens,
    })
    try:
        _ws_instance.send(msg)
        _subscribed.update(new_tokens)
        log.info(f"[WS] Abonné à {len(new_tokens)} tokens")
    except Exception as e:
        log.warning(f"[WS] Erreur subscribe: {e}")


def _on_message(ws, message: str) -> None:
    try:
        events = json.loads(message)
        if isinstance(events, dict):
            events = [events]
        for ev in events:
            if ev.get("event_type") == "price_change":
                for change in ev.get("changes", []):
                    token_id = change.get("asset_id")
                    price    = change.get("price")
                    side     = change.get("side", "")
                    if token_id and price and side == "BUY":
                        update_price(token_id, float(price))
    except Exception as e:
        log.debug(f"[WS] Parse error: {e}")


def _on_error(ws, error) -> None:
    log.warning(f"[WS] Erreur: {error}")


def _on_close(ws, code, msg) -> None:
    global _ws_running
    log.info(f"[WS] Connexion fermée ({code})")
    _ws_running = False


def _on_open(ws) -> None:
    global _ws_instance, _ws_running
    _ws_instance = ws
    _ws_running  = True
    log.info("[WS] Connecté — wss://ws-subscriptions-clob.polymarket.com")
    # Re-subscribe aux tokens déjà connus
    if _subscribed:
        subscribe_tokens(list(_subscribed))


def _run_forever() -> None:
    global _ws_running
    if websocket is None:
        log.warning("[WS] websocket-client non installé — pip install websocket-client")
        return
    backoff = 5
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=_on_open,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            log.warning(f"[WS] Crash: {e}")
        _ws_running = False
        log.info(f"[WS] Reconnexion dans {backoff}s...")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


def start_ws_feed() -> threading.Thread:
    """Lance le feed WS en thread daemon — appelé une fois au démarrage."""
    t = threading.Thread(target=_run_forever, daemon=True, name="ws-feed")
    t.start()
    log.info("[WS] Thread démarré")
    return t


def is_running() -> bool:
    return _ws_running