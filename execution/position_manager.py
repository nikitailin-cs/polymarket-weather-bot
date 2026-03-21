"""
Gestionnaire de positions ouvertes.
- Surveille les marchés en attente de résolution
- Ferme les positions automatiquement quand un marché se résout
- Tourne en parallèle de la boucle principale
"""
from __future__ import annotations
import json, sys, os, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone
from typing import List, Dict, Any
from pathlib import Path
from monitoring.pnl_tracker import _load, _save, close_position, print_open_positions
from monitoring.logger import log

POSITIONS_FILE = Path(__file__).parent.parent / "data" / "logs" / "positions.json"
GAMMA_URL      = "https://gamma-api.polymarket.com/markets"

def get_open_positions() -> list:
    from monitoring.pnl_tracker import get_open_positions as _get
    return _get()

def _fetch_market_status(condition_id: str) -> Dict:
    """Récupère le statut actuel d'un marché via Gamma API."""
    try:
        url = f"{GAMMA_URL}?conditionId={condition_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if isinstance(data, list) and data:
            return data[0]
        return {}
    except Exception as e:
        log.warning(f"Impossible de vérifier marché {condition_id[:16]}... : {e}")
        return {}


def check_and_close_positions() -> int:
    """
    Vérifie toutes les positions ouvertes.
    Ferme celles dont le marché est résolu.
    Retourne le nombre de positions fermées.
    """
    positions = _load(POSITIONS_FILE)
    if not positions:
        return 0

    closed_count = 0
    now = datetime.now(timezone.utc)

    for pos in positions:
        condition_id = pos.get("condition_id", "")
        question     = pos.get("question", "")[:50]
        end_date_str = pos.get("end_date") or ""
        side         = pos.get("side", "YES")
        size_usd     = float(pos.get("size_usd", 0))
        price        = float(pos.get("price", 0.5))
        dry_run      = pos.get("dry_run", True)

        # Vérifie si la date d'expiry est passée
        try:
            end_dt = datetime.strptime(end_date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if end_dt > now:
                continue  # Pas encore expiré
        except Exception:
            continue

        # Marché expiré — vérifie la résolution via API
        market = _fetch_market_status(condition_id)
        if not market:
            continue

        closed   = market.get("closed", False)
        outcomes = market.get("outcomes") or []
        prices   = market.get("outcomePrices") or []

        if not closed:
            log.info(f"Marché expiré mais pas encore résolu : {question}...")
            continue

        # Détermine le résultat
        try:
            import json as _json
            if isinstance(outcomes, str):
                outcomes = _json.loads(outcomes)
            if isinstance(prices, str):
                prices = _json.loads(prices)
            prices_f = [float(p) for p in prices]
        except Exception:
            continue

        # YES token = index 0, NO token = index 1
        yes_resolved = prices_f[0] >= 0.99 if prices_f else False
        no_resolved  = prices_f[1] >= 0.99 if len(prices_f) > 1 else False

        if side == "YES":
            won = yes_resolved
        else:
            won = no_resolved

        if not (yes_resolved or no_resolved):
            log.info(f"Résolution ambiguë pour {question}...")
            continue

        # Calcul PnL
        if won:
            # On a payé `price` par token, on reçoit 1.0
            pnl = size_usd * (1.0 / price - 1.0)
            result = "win"
        else:
            pnl    = -size_usd
            result = "loss"

        # Frais taker 2% sur la mise initiale
        if not dry_run:
            pnl -= size_usd * 0.02

        pnl = round(pnl, 4)

        emoji = "✅" if won else "❌"
        log.info(f"{emoji} Position fermée | {side} | {result} | PnL=${pnl:+.2f} | {question}...")

        close_position(condition_id, result, pnl)
        closed_count += 1

    return closed_count


def position_monitor_cycle() -> None:
    """À appeler dans la boucle principale à chaque cycle."""
    positions = _load(POSITIONS_FILE)
    if not positions:
        return

    log.info(f"📊 {len(positions)} position(s) ouverte(s) — vérification...")
    closed = check_and_close_positions()
    if closed > 0:
        log.info(f"   {closed} position(s) fermée(s) ce cycle")


if __name__ == "__main__":
    print_open_positions()
    print("\nVérification des résolutions...")
    n = check_and_close_positions()
    print(f"{n} position(s) fermée(s)")