"""
Gestionnaire de positions ouvertes.

v2 : machine d'états explicite à 5 états.
     ACTIVE → CLOSED_PENDING → RESOLVED_YES / RESOLVED_NO / CANCELLED
     Poll oracle toutes les 30 min uniquement pendant CLOSED_PENDING.
     PnL correct pour les 3 cas de résolution (YES, NO, remboursement partiel).
     Alerte Telegram sur CANCELLED ou timeout oracle.
"""
from __future__ import annotations
import json, sys, os, urllib.request, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from pathlib import Path
from monitoring.pnl_tracker import _load, _save, close_position, print_open_positions
from monitoring.logger import log
from core.models import MarketStatus

GAMMA_URL = "https://gamma-api.polymarket.com/markets"

# Timestamps en mémoire du dernier poll oracle par condition_id
_last_poll: dict = {}


def get_open_positions() -> list:
    from monitoring.pnl_tracker import get_open_positions as _get
    return _get()


# ── Appel Gamma API ───────────────────────────────────────────────────────────

def _fetch_market(condition_id: str) -> Dict:
    """
    Récupère les données d un marché Polymarket via Gamma API.
    Essaie plusieurs variantes de query pour maximiser les chances
    de trouver un marché expiré ou résolu.
    """
    # Gamma supporte conditionId et conditionIds (pluriel) selon les versions
    variants = [
        f"{GAMMA_URL}?conditionId={condition_id}",
        f"{GAMMA_URL}?conditionIds={condition_id}",
    ]
    for url in variants:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            if isinstance(data, list) and data:
                market = data[0]
                log.debug(
                    f"Gamma [{condition_id[:16]}]: "
                    f"active={market.get('active')} "
                    f"closed={market.get('closed')} "
                    f"prices={market.get('outcomePrices')}"
                )
                return market
        except Exception as e:
            log.debug(f"Gamma variant [{url[-30:]}] erreur: {e}")
            continue
    log.warning(f"Gamma: aucune donnée pour {condition_id[:16]} (marché introuvable ou API down)")
    return {}


# ── Interprétation de la réponse Gamma ───────────────────────────────────────

def _read_status(data: dict) -> Optional[MarketStatus]:
    """
    Traduit la réponse Gamma en MarketStatus.

    Règles :
      active=True                          → None (toujours actif, pas de changement)
      active=False + prices[0] >= 0.99    → RESOLVED_YES
      active=False + prices[0] <= 0.01    → RESOLVED_NO
      active=False + closed + prix ~0.50  → CANCELLED  (litige UMA)
      active=False + prix intermédiaire   → CLOSED_PENDING
    """
    if data.get("active", True):
        return None

    closed = data.get("closed", False)
    raw    = data.get("outcomePrices") or []

    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        prices = [float(p) for p in raw]
    except Exception:
        prices = []

    if not prices:
        return MarketStatus.CLOSED_PENDING

    p_yes = prices[0]

    if p_yes >= 0.99:
        return MarketStatus.RESOLVED_YES
    if p_yes <= 0.01:
        return MarketStatus.RESOLVED_NO
    if closed and 0.45 <= p_yes <= 0.55:
        return MarketStatus.CANCELLED    # prix ~0.5 sur marché fermé = litige

    return MarketStatus.CLOSED_PENDING


def _resolution_price(status: MarketStatus, side: str, data: dict) -> float:
    """
    Prix de résolution pour le calcul du PnL.
    - YES résolu, position YES  → 1.0  (gain)
    - NO résolu,  position YES  → 0.0  (perte totale)
    - CANCELLED                 → prix de remboursement (outcomePrices[0])
    """
    if status == MarketStatus.CANCELLED:
        try:
            raw = data.get("outcomePrices") or ["0.5"]
            if isinstance(raw, str):
                raw = json.loads(raw)
            return float(raw[0])
        except Exception:
            return 0.5

    if status == MarketStatus.RESOLVED_YES:
        return 1.0 if side == "YES" else 0.0
    if status == MarketStatus.RESOLVED_NO:
        return 0.0 if side == "YES" else 1.0

    return 0.5  # ne devrait pas arriver


# ── Alerte Telegram légère ────────────────────────────────────────────────────

def _telegram(msg: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat  = os.getenv("TELEGRAM_CHAT_ID",   "")
    if not token or not chat:
        return
    try:
        import urllib.parse
        payload = json.dumps({"chat_id": chat, "text": msg, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


# ── Cycle principal ───────────────────────────────────────────────────────────

def position_monitor_cycle(spec_cache=None) -> None:
    """
    À appeler à chaque cycle depuis main.py.

    Logique par position :
      1. Si end_date pas encore dépassée → rien à faire
      2. Si dépassée ET dernier poll < 30 min → rien à faire (économie réseau)
      3. Poll Gamma → interprète le statut
      4. Si terminal (YES/NO/CANCELLED) → ferme la position + calcule PnL
      5. Si CLOSED_PENDING → met à jour le timestamp, reprogramme le prochain poll
      6. Si CLOSED_PENDING depuis > MAX_ORACLE_WAIT_HOURS → alerte Telegram
    """
    from config.settings import ORACLE_POLL_INTERVAL_SEC, MAX_ORACLE_WAIT_HOURS

    positions = _load(_positions_path())
    if not positions:
        return

    log.info(f"📊 {len(positions)} position(s) ouverte(s) — vérification...")
    now_mono = time.monotonic()
    now_dt   = datetime.now(timezone.utc)
    closed_n = 0

    for pos in positions:
        condition_id = pos.get("condition_id", "")
        question     = pos.get("question", "")[:50]
        end_date_str = pos.get("end_date") or ""
        side         = pos.get("side", "YES")
        size_usd     = float(pos.get("size_usd", 0))
        price_entry  = float(pos.get("price", 0.5))
        dry_run      = pos.get("dry_run", True)
        pos_status   = pos.get("market_status", MarketStatus.ACTIVE.value)

        # ── 1. Déjà résolu côté cache → fermeture directe ────────────────────
        if pos_status in (
            MarketStatus.RESOLVED_YES.value,
            MarketStatus.RESOLVED_NO.value,
            MarketStatus.CANCELLED.value,
        ):
            # Ne devrait pas arriver (fermé au cycle précédent), mais sécurité
            log.warning(f"Position {condition_id[:16]} marquée {pos_status} mais encore ouverte — correction")
            _close_pos(pos, MarketStatus(pos_status), {}, condition_id, side, size_usd, price_entry, dry_run)
            closed_n += 1
            continue

        # ── 2. Vérifie si end_date dépassée ──────────────────────────────────
        try:
            end_dt = datetime.strptime(end_date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if end_dt > now_dt:
                continue   # pas encore expiré
        except Exception:
            pass   # date inconnue → on poll quand même

        # ── 3. Throttle : pas plus d'1 poll toutes les ORACLE_POLL_INTERVAL_SEC ──
        last = _last_poll.get(condition_id, None)
        if last is not None and (now_mono - last) < ORACLE_POLL_INTERVAL_SEC:
            continue

        _last_poll[condition_id] = now_mono

        # ── 4. Poll Gamma ─────────────────────────────────────────────────────
        data       = _fetch_market(condition_id)
        new_status = _read_status(data)

        if new_status is None:
            # Deux cas possibles :
            # a) Marché toujours actif selon Gamma (end_date approximatif ou oracle en attente)
            # b) _fetch_market a retourné {} car conditionId introuvable
            #
            # Si end_date est passée depuis > 12h et le marché reste introuvable,
            # on force CLOSED_PENDING pour déclencher le timeout oracle.
            try:
                end_dt_check = datetime.strptime(end_date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                hours_overdue = (now_dt - end_dt_check).total_seconds() / 3600
                if hours_overdue > 12 and not data:
                    log.info(
                        f"Position {condition_id[:16]} introuvable sur Gamma "                        f"({hours_overdue:.0f}h après expiry) → CLOSED_PENDING forcé"
                    )
                    new_status = MarketStatus.CLOSED_PENDING
                else:
                    continue
            except Exception:
                continue

        # ── 5. Mise à jour du statut dans la position (pour spec_cache) ───────
        if spec_cache is not None:
            spec_cache.update_status(
                condition_id,
                new_status,
                last_oracle_poll = now_dt.isoformat(),
            )

        # ── 6. Timeout oracle ─────────────────────────────────────────────────
        if new_status == MarketStatus.CLOSED_PENDING:
            first_poll_str = pos.get("first_pending_at", "")
            if not first_poll_str:
                # Premier cycle CLOSED_PENDING → on enregistre l'heure
                _mark_first_pending(condition_id, now_dt.isoformat())
            else:
                try:
                    first_dt  = datetime.fromisoformat(first_poll_str)
                    elapsed_h = (now_dt - first_dt).total_seconds() / 3600
                    if elapsed_h > MAX_ORACLE_WAIT_HOURS:
                        msg = (
                            f"⚠️ *Oracle timeout* `{condition_id[:16]}`\n"
                            f"{elapsed_h:.0f}h sans résolution — vérification manuelle requise\n"
                            f"_{question}_"
                        )
                        log.error(msg.replace("*", "").replace("`", "").replace("_", ""))
                        _telegram(msg)
                except Exception:
                    pass
            log.info(f"En attente oracle : {question}…")
            continue

        # ── 7. Terminal → ferme la position ───────────────────────────────────
        _close_pos(pos, new_status, data, condition_id, side, size_usd, price_entry, dry_run)
        closed_n += 1

    if closed_n:
        log.info(f"   {closed_n} position(s) fermée(s) ce cycle")


def _close_pos(
    pos:          dict,
    status:       MarketStatus,
    data:         dict,
    condition_id: str,
    side:         str,
    size_usd:     float,
    price_entry:  float,
    dry_run:      bool,
) -> None:
    """Calcule le PnL, ferme la position et notifie."""
    res_price = _resolution_price(status, side, data)

    # PnL = (prix_résolution − prix_entrée) × nombre_de_tokens
    nb_tokens = size_usd / price_entry if price_entry > 0 else 0
    pnl       = round((res_price - price_entry) * nb_tokens, 4)

    result  = "win" if pnl >= 0 else "loss"
    emoji   = "✅" if pnl >= 0 else "❌"
    question = pos.get("question", "")[:50]

    log.info(
        f"{emoji} Fermé [{side}] {status.value} | "
        f"PnL={pnl:+.2f}$ ({price_entry:.3f}→{res_price:.3f}) | {question}…"
    )

    close_position(condition_id, result, pnl)

    # Notification Telegram pour CANCELLED ou pertes importantes
    if status == MarketStatus.CANCELLED or pnl < -10:
        msg_parts = [
            f"{emoji} *Position fermée* `{condition_id[:16]}`",
            f"Statut: `{status.value}`",
            f"PnL: `{pnl:+.2f}$`",
            f"_{question}_",
        ]
        if status == MarketStatus.CANCELLED:
            msg_parts.append(f"⚠️ Remboursement partiel à {res_price:.3f}$")
        _telegram("\n".join(msg_parts))


def _positions_path() -> Path:
    from config.settings import POSITIONS_FILE
    return Path(POSITIONS_FILE)


def _mark_first_pending(condition_id: str, iso_str: str) -> None:
    """Enregistre l'heure du premier CLOSED_PENDING dans le fichier positions."""
    path      = _positions_path()
    positions = _load(path)
    for pos in positions:
        if pos.get("condition_id") == condition_id:
            pos["first_pending_at"]  = iso_str
            pos["market_status"]     = MarketStatus.CLOSED_PENDING.value
    _save(path, positions)


def check_and_close_positions() -> int:
    """
    Alias public pour rétrocompatibilité.
    Retourne le nombre de positions fermées ce cycle.
    """
    before = len(get_open_positions())
    position_monitor_cycle()
    after  = len(get_open_positions())
    return before - after


if __name__ == "__main__":
    print_open_positions()
    print("\nVérification des résolutions...")
    n = check_and_close_positions()
    print(f"{n} position(s) fermée(s)")