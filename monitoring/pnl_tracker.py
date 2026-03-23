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
    condition_id:   str,
    token_id:       str,
    question:       str,
    side:           str,
    size_usd:       float,          # taille effectivement remplie
    price:          float,
    edge:           float,
    our_prob:       float,
    market_prob:    float,
    confidence:     float,
    end_date:       Optional[str],
    dry_run:        bool = True,
    order_id:       Optional[str] = None,
    size_requested: Optional[float] = None,   # taille demandée (avant fill partiel)
    fill_ratio:     float = 1.0,              # 1.0 = fill complet, 0.6 = 60% rempli
) -> Dict:
    POSITIONS_FILE, TRADES_FILE, _ = _get_paths()
    now = datetime.now(timezone.utc).isoformat()

    trade = {
        "id":             order_id or f"{'DRY' if dry_run else 'LIVE'}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "timestamp":      now,
        "condition_id":   condition_id,
        "token_id":       token_id,
        "question":       question,
        "side":           side,
        "size_usd":       size_usd,
        "size_requested": size_requested or size_usd,
        "fill_ratio":     round(fill_ratio, 4),
        "price":          price,
        "edge_pct":       round(edge * 100, 2),
        "our_prob":       round(our_prob * 100, 2),
        "market_prob":    round(market_prob * 100, 2),
        "confidence":     round(confidence * 100, 2),
        "end_date":       end_date,
        "dry_run":        dry_run,
        "status":         "open",
        "result":         None,
        "pnl_usd":        None,
        "closed_at":      None,
        # Fonds bloqués DVM : rempli quand la position entre en dispute UMA
        "dvm_disputed":   False,    # True = vote DVM en cours
        "dvm_blocked_usd": 0.0,     # USDC réels bloqués dans le contrat CTF
        "dvm_bond_usd":    0.0,     # bond du bot engagé dans la dispute
        "dvm_since":       None,    # ISO timestamp de début de dispute
    }

    trades = _load(TRADES_FILE)
    trades.append(trade)
    _save(TRADES_FILE, trades)

    positions = _load(POSITIONS_FILE)
    positions.append(trade)
    _save(POSITIONS_FILE, positions)

    return trade


def mark_dvm_disputed(
    condition_id: str,
    bond_usd:     float = 0.0,
) -> None:
    """
    Marque une position comme bloquée en vote DVM UMA.
    Les fonds sont physiquement gelés dans le contrat CTF
    jusqu'à résolution du vote (4-6 jours).

    dvm_blocked_usd = fonds de la position (récupérables si DVM vote juste)
    dvm_bond_usd    = bond engagé par le bot dans la dispute (perdu si DVM contre)
    """
    POSITIONS_FILE, TRADES_FILE, _ = _get_paths()
    now = datetime.now(timezone.utc).isoformat()

    for path in (POSITIONS_FILE, TRADES_FILE):
        data = _load(path)
        changed = False
        for entry in data:
            if entry.get("condition_id") == condition_id and entry.get("status") == "open":
                entry["dvm_disputed"]   = True
                entry["dvm_blocked_usd"] = float(entry.get("size_usd", 0))
                entry["dvm_bond_usd"]   = bond_usd
                entry["dvm_since"]      = now
                changed = True
        if changed:
            _save(path, data)


def get_dvm_blocked_capital() -> dict:
    """
    Retourne le capital bloqué dans des votes DVM en cours.
    Utilisé par get_current_bankroll() pour scinder la bankroll.

    Returns:
        {
          "total_blocked_usd":  float,   # USDC gelés dans CTF
          "total_bond_usd":     float,   # bonds engagés (à risque)
          "positions": [...]             # liste des positions bloquées
        }
    """
    POSITIONS_FILE, _, _ = _get_paths()
    positions = _load(POSITIONS_FILE)
    blocked = [p for p in positions if p.get("dvm_disputed")]
    return {
        "total_blocked_usd": sum(p.get("dvm_blocked_usd", 0) for p in blocked),
        "total_bond_usd":    sum(p.get("dvm_bond_usd", 0)    for p in blocked),
        "count":             len(blocked),
        "positions":         blocked,
    }


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
    """
    Retourne la bankroll disponible = initial + PnL réalisé.
    N'inclut PAS les fonds bloqués en vote DVM.

    Pour la bankroll complète (disponible + bloquée) :
        from monitoring.pnl_tracker import get_bankroll_detail
    """
    _, _, PNL_FILE = _get_paths()
    pnl_data = _load(PNL_FILE)
    realized = pnl_data.get("total_pnl_usd", 0.0) if isinstance(pnl_data, dict) else 0.0
    return round(initial + realized, 2)


def get_bankroll_detail(initial: float = 150.0) -> dict:
    """
    Bankroll scindée selon la nature des fonds.

    Returns:
        {
          "disponible":   float,  # USDC librement tradables
          "bloquee_dvm":  float,  # gelés en vote DVM (récupérables)
          "bond_risque":  float,  # bonds à risque de confiscation
          "total":        float,  # total théorique
          "realised_pnl": float,
        }
    """
    _, _, PNL_FILE = _get_paths()
    pnl_data = _load(PNL_FILE)
    realized = pnl_data.get("total_pnl_usd", 0.0) if isinstance(pnl_data, dict) else 0.0

    dvm = get_dvm_blocked_capital()
    blocked = dvm["total_blocked_usd"]
    bond    = dvm["total_bond_usd"]

    disponible = round(initial + realized, 2)
    total      = round(disponible + blocked + bond, 2)

    return {
        "disponible":   disponible,
        "bloquee_dvm":  round(blocked, 2),
        "bond_risque":  round(bond, 2),
        "total":        total,
        "realised_pnl": round(realized, 2),
        "dvm_positions": dvm["count"],
    }


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

    # Affiche les fonds bloqués DVM si applicable
    dvm = get_dvm_blocked_capital()
    if dvm["count"] > 0:
        print(f"{'─'*72}")
        print(f"  ⚠️  Fonds bloqués DVM UMA ({dvm['count']} position(s))")
        print(f"     Capital gelé  : ${dvm['total_blocked_usd']:+.2f} (récupérable)")
        print(f"     Bonds à risque: ${dvm['total_bond_usd']:+.2f} (confisqués si DVM défavorable)")
        print(f"     Coût opportunité estimé : impact sur rotation portefeuille")
    print(f"{'═'*72}")


if __name__ == "__main__":
    print_open_positions()
    print_pnl_summary()