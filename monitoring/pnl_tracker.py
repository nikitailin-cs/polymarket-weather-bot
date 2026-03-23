"""
Tracker PnL — enregistre chaque trade et calcule les performances.
Persistance JSON dans data/logs/ — survit aux crashes.

Utilise les chemins absolus depuis config/settings pour cohérence
avec position_manager (évite toute ambiguïté selon le CWD).
"""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from pathlib import Path


def _get_paths():
    """Charge les chemins depuis settings — import tardif pour éviter les circularités."""
    try:
        from config.settings import POSITIONS_FILE, TRADES_FILE, PNL_FILE
        return Path(POSITIONS_FILE), Path(TRADES_FILE), Path(PNL_FILE)
    except Exception:
        # Fallback si settings pas encore chargé (tests unitaires)
        base = Path(__file__).parent.parent / "data" / "logs"
        return base / "positions.json", base / "trades.json", base / "pnl.json"


def _load(path: Path) -> Any:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


# ── Enregistrement des trades ─────────────────────────────────────────────────

def record_trade(
    condition_id:  str,
    token_id:      str,
    question:      str,
    side:          str,
    size_usd:      float,
    price:         float,
    edge:          float,
    our_prob:      float,
    market_prob:   float,
    confidence:    float,
    end_date:      Optional[str],
    dry_run:       bool = True,
    order_id:      Optional[str] = None,
) -> Dict:
    POSITIONS_FILE, TRADES_FILE, _ = _get_paths()
    now = datetime.now(timezone.utc).isoformat()

    trade = {
        "id":           order_id or f"{'DRY' if dry_run else 'LIVE'}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "timestamp":    now,
        "condition_id": condition_id,
        "token_id":     token_id,
        "question":     question,
        "side":         side,
        "size_usd":     size_usd,
        "price":        price,
        "edge_pct":     round(edge * 100, 2),
        "our_prob":     round(our_prob * 100, 2),
        "market_prob":  round(market_prob * 100, 2),
        "confidence":   round(confidence * 100, 2),
        "end_date":     end_date,
        "dry_run":      dry_run,
        "status":       "open",
        "result":       None,
        "pnl_usd":      None,
        "closed_at":    None,
    }

    trades = _load(TRADES_FILE)
    trades.append(trade)
    _save(TRADES_FILE, trades)

    positions = _load(POSITIONS_FILE)
    positions.append(trade)
    _save(POSITIONS_FILE, positions)

    return trade


def close_position(condition_id: str, result: str, pnl_usd: float) -> None:
    """
    Ferme une position quand le marché se résout.
    result = "win" / "loss"
    pnl_usd = profit ou perte en dollars
    """
    POSITIONS_FILE, TRADES_FILE, _ = _get_paths()
    now = datetime.now(timezone.utc).isoformat()

    trades = _load(TRADES_FILE)
    for t in trades:
        if t["condition_id"] == condition_id and t["status"] == "open":
            t["status"]    = "closed"
            t["result"]    = result
            t["pnl_usd"]   = round(pnl_usd, 4)
            t["closed_at"] = now
    _save(TRADES_FILE, trades)

    positions = _load(POSITIONS_FILE)
    positions = [p for p in positions if p["condition_id"] != condition_id]
    _save(POSITIONS_FILE, positions)

    _update_pnl(pnl_usd, result)


def _update_pnl(pnl_usd: float, result: str) -> None:
    _, _, PNL_FILE = _get_paths()
    pnl = _load(PNL_FILE) or {
        "total_trades": 0,
        "wins":         0,
        "losses":       0,
        "total_pnl_usd": 0.0,
        "best_trade":   0.0,
        "worst_trade":  0.0,
        "history":      [],
    }
    pnl["total_trades"]  += 1
    pnl["total_pnl_usd"]  = round(pnl["total_pnl_usd"] + pnl_usd, 4)
    if result == "win":
        pnl["wins"] += 1
    else:
        pnl["losses"] += 1
    pnl["best_trade"]  = max(pnl["best_trade"], pnl_usd)
    pnl["worst_trade"] = min(pnl["worst_trade"], pnl_usd)
    pnl["history"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pnl_usd":   round(pnl_usd, 4),
        "result":    result,
    })
    _save(PNL_FILE, pnl)


def get_current_bankroll(initial: float = 150.0) -> float:
    """Retourne la bankroll courante = initial + PnL réalisé uniquement."""
    _, _, PNL_FILE = _get_paths()
    pnl_data = _load(PNL_FILE)
    realized = pnl_data.get("total_pnl_usd", 0.0) if isinstance(pnl_data, dict) else 0.0
    return round(initial + realized, 2)


def get_open_positions() -> list:
    """Retourne les positions ouvertes."""
    POSITIONS_FILE, _, _ = _get_paths()
    return _load(POSITIONS_FILE)


# ── Affichage ─────────────────────────────────────────────────────────────────

def print_open_positions() -> None:
    positions = get_open_positions()
    if not positions:
        print("  Aucune position ouverte.")
        return
    now = datetime.now(timezone.utc)
    print(f"\n{'═'*72}")
    print(f"  POSITIONS OUVERTES ({len(positions)})")
    print(f"{'═'*72}")
    for p in positions:
        end   = p.get("end_date") or "?"
        size  = p.get("size_usd", 0)
        side  = p.get("side", "?")
        price = p.get("price", 0)
        edge  = p.get("edge_pct", 0)
        dry   = " [DRY]" if p.get("dry_run") else " [LIVE]"
        ts    = (p.get("timestamp") or "")[:16].replace("T", " ")
        try:
            end_dt    = datetime.strptime(end[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_left = (end_dt - now).days
            time_str  = f"{days_left}j restants"
        except Exception:
            time_str = "?"
        print(f"\n  [{side}]{dry} ${size:.2f} @ {price:.3f}  edge={edge:.1f}%")
        print(f"  Ouvert : {ts}  |  Expire : {end[:10]} ({time_str})")
        print(f"  {p.get('question', '')[:65]}...")
    print(f"{'═'*72}")


def print_pnl_summary() -> None:
    _, _, PNL_FILE = _get_paths()
    pnl = _load(PNL_FILE)
    if not pnl or not isinstance(pnl, dict):
        print("  Aucun trade clôturé pour l'instant.")
        return

    total   = pnl.get("total_trades", 0)
    wins    = pnl.get("wins", 0)
    losses  = pnl.get("losses", 0)
    net     = pnl.get("total_pnl_usd", 0.0)
    best    = pnl.get("best_trade", 0.0)
    worst   = pnl.get("worst_trade", 0.0)
    winrate = (wins / total * 100) if total > 0 else 0

    print(f"\n{'═'*72}")
    print(f"  RÉSUMÉ PnL")
    print(f"{'═'*72}")
    print(f"  Trades clôturés : {total}  (✅ {wins} wins  ❌ {losses} losses)")
    print(f"  Winrate         : {winrate:.1f}%")
    print(f"  PnL net         : ${net:+.2f}")
    print(f"  Meilleur trade  : ${best:+.2f}")
    print(f"  Pire trade      : ${worst:+.2f}")
    print(f"{'═'*72}")


if __name__ == "__main__":
    print_open_positions()
    print_pnl_summary()