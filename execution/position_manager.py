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
from execution.uma_oracle import (
    get_uma_state, get_uma_status,
    UMAPhase, UMAStatus, UMAState,
)
from execution.ctf_manager import (
    is_condition_resolved, redeem_position, RedeemResult, get_usdc_balance
)
from monitoring.onchain_logger import (
    log_resolution_check, log_redeem, log_uma_dispute
)
from execution.uma_disputer import (
    check_and_dispute_if_needed, DisputeResult
)
from monitoring.pnl_tracker import mark_dvm_disputed

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

    # Champ "disputed" exposé par Gamma API — niveau 1 (sans RPC)
    if data.get("disputed", False):
        return MarketStatus.CANCELLED

    if closed and 0.45 <= p_yes <= 0.55:
        return MarketStatus.CANCELLED    # prix ~0.5 sur marché fermé = litige UMA

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

def _verify_resolution_with_contract(
    condition_id: str,
    gamma_status: Optional[MarketStatus],
    spec_cache,
    pos: dict,
) -> Optional[MarketStatus]:
    """
    Cross-check indépendant : compare la résolution Gamma avec
    ContractSpec.evaluate() appliquée à la température réellement observée.

    Logique :
      1. Récupère le ContractSpec depuis le cache (threshold_op, threshold_value, city, date)
      2. Récupère la température observée via Open-Meteo Archive (source de vérité météo)
      3. Applique spec.evaluate(observed_temp) → YES ou NO contractuellement
      4. Compare avec gamma_status
      5. En cas de conflit → log + alerte, mais garde gamma_status (Gamma = source financière)

    Retourne :
      - gamma_status inchangé si tout concorde ou si données manquantes
      - gamma_status inchangé + log de conflit si divergence détectée
    """
    if spec_cache is None or gamma_status is None:
        return gamma_status

    # Uniquement sur les résolutions terminales YES/NO (pas CANCELLED/PENDING)
    if gamma_status not in (MarketStatus.RESOLVED_YES, MarketStatus.RESOLVED_NO):
        return gamma_status

    spec = spec_cache.get(condition_id)
    if spec is None:
        return gamma_status

    # Ne traite que les marchés de température max (extension possible aux autres)
    if spec.weather_variable not in ("temp_max", "temp_min", "temp_avg"):
        return gamma_status

    city     = spec.city
    end_date = (spec.end_date_iso or pos.get("end_date") or "")[:10]
    if not city or not end_date:
        return gamma_status

    # Récupère la température réellement observée
    try:
        from signals.sources.open_meteo import get_max_temp, get_min_temp
        if spec.weather_variable == "temp_max":
            observed = get_max_temp(city, end_date)
        elif spec.weather_variable == "temp_min":
            observed = get_min_temp(city, end_date)
        else:
            observed = get_max_temp(city, end_date)   # avg : approximation

        if observed is None:
            log.debug(
                f"_verify_resolution: pas de température observée pour "
                f"{city} {end_date} — skip cross-check"
            )
            log_resolution_check(
                condition_id=condition_id, source="ctf_evaluate",
                resolved=True, gamma_status=gamma_status.value,
                observed_value=None,
            )
            return gamma_status

        # Applique la condition contractuelle
        contract_says_yes = spec.evaluate(observed)
        contract_status   = (
            MarketStatus.RESOLVED_YES if contract_says_yes
            else MarketStatus.RESOLVED_NO
        )

        conflict = (contract_status != gamma_status)

        log_resolution_check(
            condition_id               = condition_id,
            source                     = "ctf_evaluate",
            resolved                   = True,
            resolution_matches_contract= not conflict,
            observed_value             = observed,
            contract_threshold         = spec.threshold_value,
            contract_op                = spec.threshold_op,
            gamma_status               = gamma_status.value,
            conflict                   = conflict,
        )

        if conflict:
            # Conflit : Gamma dit YES mais le contrat dit NO (ou vice-versa)
            # Causes possibles : arrondi différent, station météo différente,
            # litige UMA en cours non encore reflété dans Gamma.
            log.warning(
                f"🚨 CONFLIT RÉSOLUTION [{condition_id[:16]}] | "
                f"Gamma={gamma_status.value} | "
                f"ContractSpec.evaluate({observed:.1f}°C)={contract_status.value} | "
                f"seuil={spec.threshold_op} {spec.threshold_value}{spec.threshold_unit} | "
                f"{city} {end_date}"
            )
            _telegram(
                f"🚨 *Conflit résolution* `{condition_id[:16]}`\n"
                f"Gamma: `{gamma_status.value}` | "
                f"Contrat: `{contract_status.value}`\n"
                f"Observé: `{observed:.1f}°C` vs seuil `{spec.threshold_op} "
                f"{spec.threshold_value}{spec.threshold_unit}`\n"
                f"_{city} {end_date} — vérification manuelle recommandée_"
            )
            # On garde gamma_status : c'est lui qui détermine le payout réel.
            # Le conflit est loggé pour audit.
        else:
            log.info(
                f"✅ Cross-check contractuel OK [{condition_id[:16]}] | "
                f"observe={observed:.1f}°C → {spec.threshold_op} "
                f"{spec.threshold_value} → {contract_status.value} = Gamma"
            )

    except Exception as e:
        log.debug(f"_verify_resolution erreur [{condition_id[:16]}]: {e}")

    return gamma_status   # toujours gamma_status — evaluate() est advisory


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

    # ── Cross-check balance USDC on-chain (1 fois par cycle) ─────────────────
    # Vérifie que la bankroll on-chain est cohérente avec notre comptabilité.
    # Un écart > 5$ signale une anomalie (fill silencieux, redeem manqué, etc.)
    try:
        from config.settings import MAX_PORTFOLIO_USD
        usdc_onchain = get_usdc_balance()
        if usdc_onchain > 0:
            from monitoring.pnl_tracker import get_current_bankroll
            bankroll_bot = get_current_bankroll(MAX_PORTFOLIO_USD)
            # La balance on-chain inclut les fonds engagés dans les positions
            # ouvertes → on compare avec tolérance large (positions ouvertes ≠ USDC libre)
            log.debug(f"USDC on-chain={usdc_onchain:.2f}$ | bankroll bot={bankroll_bot:.2f}$")
    except Exception:
        pass  # non-bloquant
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

        # ── 3. Throttle dynamique selon la phase UMA ─────────────────────────
        # Le poll interval s'adapte à la phase UMA réelle (depuis le cache) :
        #   AWAITING_PROPOSAL → 30min (proposition attendue mais pas urgente)
        #   IN_DISPUTE_WINDOW → 1-10min (fenêtre critique, surveiller de près)
        #   SETTLEABLE        → 5min   (settle() imminent, Gamma va se mettre à jour)
        #   DISPUTED          → 4h     (vote UMA = jours, inutile de poller souvent)
        #   défaut            → ORACLE_POLL_INTERVAL_SEC (30min)
        cached_uma = _state_cache.get(condition_id) if "_state_cache" in dir() else None
        # Import du cache UMA directement
        try:
            from execution.uma_oracle import _state_cache as _uma_cache
            cached_uma_state = _uma_cache.get(condition_id)
            dynamic_interval = (
                cached_uma_state.recommended_poll_interval_sec
                if cached_uma_state else ORACLE_POLL_INTERVAL_SEC
            )
        except Exception:
            dynamic_interval = ORACLE_POLL_INTERVAL_SEC

        last = _last_poll.get(condition_id, None)
        if last is not None and (now_mono - last) < dynamic_interval:
            continue

        _last_poll[condition_id] = now_mono

        # ── 4. Poll Gamma ─────────────────────────────────────────────────────
        data       = _fetch_market(condition_id)
        new_status = _read_status(data)

        # ── Cross-check contractuel (ContractSpec.evaluate) ───────────────────
        # Compare la décision Gamma avec evaluate(température_observée).
        # Ne modifie pas new_status — advisory uniquement (log + alerte si conflit).
        if new_status in (MarketStatus.RESOLVED_YES, MarketStatus.RESOLVED_NO):
            new_status = _verify_resolution_with_contract(
                condition_id, new_status, spec_cache, pos
            )

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

        # ── 5. Surveillance UMA on-chain — séquence temporelle complète ────────
        # Interroge l'OO UMA avec la phase exacte (AWAITING_PROPOSAL /
        # IN_DISPUTE_WINDOW / SETTLEABLE / DISPUTED / SETTLED) pour adapter
        # dynamiquement le throttle de poll et le timeout d'alerte.
        uma_state: Optional[UMAState] = None
        if new_status == MarketStatus.CLOSED_PENDING:
            # Passe le timestamp de fermeture pour calculer AWAITING_PROPOSAL
            market_closed_ts = 0
            try:
                end_dt_uma = datetime.strptime(end_date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                market_closed_ts = int(end_dt_uma.timestamp())
            except Exception:
                pass

            uma_state = get_uma_state(condition_id, market_closed_at=market_closed_ts)
            phase     = uma_state.phase

            if phase == UMAPhase.DISPUTED:
                log.warning(
                    f"🚨 DISPUTE UMA [{condition_id[:16]}] "
                    f"proposer={uma_state.proposer[:10]} "
                    f"disputer={uma_state.disputer[:10]} "
                    f"— résolution bloquée jusqu'à 7j"
                )
                log_uma_dispute(condition_id, phase.value,
                                proposer=uma_state.proposer,
                                disputer=uma_state.disputer)

                # Marque les fonds comme bloqués dans le vote DVM
                # Les USDC sont physiquement gelés dans le contrat CTF
                # jusqu'à résolution (4-6 jours) — coût d'opportunité réel
                mark_dvm_disputed(condition_id, bond_usd=uma_state.bond_usdc)

                _telegram(
                    f"🚨 *Dispute UMA — fonds bloqués* `{condition_id[:16]}`\n"
                    f"Proposer: `{uma_state.proposer[:16]}`\n"
                    f"Disputer: `{uma_state.disputer[:16]}`\n"
                    f"Capital gelé: `${size_usd:.2f}` | Bond: `${uma_state.bond_usdc:.0f}`\n"
                    f"Résolution DVM dans 4-6 jours\n_{question}_"
                )
                # On NE force plus CANCELLED immédiatement —
                # les fonds sont bloqués mais pas perdus.
                # La position reste CLOSED_PENDING jusqu'au verdict DVM.
                # Le coût d'opportunité est modélisé dans get_bankroll_detail().
                log.info(
                    f"Position {condition_id[:16]} maintenue CLOSED_PENDING "
                    f"pendant vote DVM | fonds bloqués=${size_usd:.2f}"
                )

            elif phase == UMAPhase.AWAITING_PROPOSAL:
                waited_min = (uma_state.seconds_since_closed or 0) // 60
                log.info(
                    f"UMA AWAITING_PROPOSAL [{condition_id[:16]}] "
                    f"— fermé il y a {waited_min}min, proposition attendue"
                )
                if (uma_state.seconds_since_closed or 0) > 3 * 3600:
                    # Pas de proposition après 3h → anomalie à signaler
                    _telegram(
                        f"⚠️ *Proposition UMA manquante* `{condition_id[:16]}`\n"
                        f"Marché fermé il y a {waited_min}min sans proposition\n"
                        f"_{question}_"
                    )

            elif phase == UMAPhase.IN_DISPUTE_WINDOW:
                rem  = uma_state.dispute_window_remaining_sec or 0
                prop = "YES" if uma_state.proposed_yes else "NO"
                log.info(
                    f"UMA IN_DISPUTE_WINDOW [{condition_id[:16]}] "
                    f"proposition={prop} "
                    f"fenêtre_expire_dans={rem//60}min "
                    f"liveness={uma_state.liveness_sec}s "
                    f"bond={uma_state.bond_usdc:.0f}USDC"
                )
                # ── Évaluation et dispute si proposition erronée ──────────────
                # Compare proposed_price UMA avec ContractSpec.evaluate()
                # Si divergence et confiance >= MIN_DISPUTE_CONFIDENCE → dispute
                from config.settings import DRY_RUN as GLOBAL_DRY_RUN
                dispute_result = check_and_dispute_if_needed(
                    condition_id = condition_id,
                    uma_state    = uma_state,
                    spec_cache   = spec_cache,
                    dry_run      = dry_run or GLOBAL_DRY_RUN,
                )
                if dispute_result == DisputeResult.DISPUTED:
                    log.warning(
                        f"🚨 Dispute déclenchée on-chain | {condition_id[:16]} | "
                        f"bond={uma_state.bond_usdc:.0f} USDC engagé | "
                        f"résolution DVM dans 4-6 jours"
                    )
                    # Marque la position comme bloquée DVM
                    mark_dvm_disputed(condition_id, bond_usd=uma_state.bond_usdc)
                elif dispute_result == DisputeResult.DRY_RUN:
                    log.info(f"[DRY-RUN] Dispute évaluée | {condition_id[:16]}")
                elif dispute_result == DisputeResult.NO_DIVERGENCE:
                    log.debug(f"Proposition UMA validée | {condition_id[:16]}")

            elif phase == UMAPhase.SETTLEABLE:
                log.info(
                    f"UMA SETTLEABLE [{condition_id[:16]}] "
                    f"— settle() appelable, Gamma devrait se mettre à jour"
                )

            elif phase == UMAPhase.SETTLED:
                log.info(
                    f"UMA SETTLED on-chain [{condition_id[:16]}] "
                    f"→ {'YES' if uma_state.resolved_price and uma_state.resolved_price > 0 else 'NO'} "
                    f"— re-poll Gamma au prochain cycle"
                )

        # ── 6. Mise à jour du statut dans la position (pour spec_cache) ───────
        if spec_cache is not None:
            spec_cache.update_status(
                condition_id,
                new_status,
                last_oracle_poll = now_dt.isoformat(),
            )

        # ── 7. Timeout oracle dynamique ───────────────────────────────────────
        # Le timeout est calculé depuis l'état UMA réel, pas une constante.
        #   AWAITING_PROPOSAL → alerte après MAX_PROPOSAL_WAIT_SEC (6h)
        #   IN_DISPUTE_WINDOW → alerte après liveness + 1h de marge
        #   SETTLEABLE        → alerte après 2h (settle() devrait être rapide)
        #   DISPUTED          → alerte après 7 jours (vote UMA)
        #   défaut            → MAX_ORACLE_WAIT_HOURS (48h)
        if new_status == MarketStatus.CLOSED_PENDING:
            first_poll_str = pos.get("first_pending_at", "")
            if not first_poll_str:
                _mark_first_pending(condition_id, now_dt.isoformat())
            else:
                try:
                    first_dt  = datetime.fromisoformat(first_poll_str)
                    elapsed_h = (now_dt - first_dt).total_seconds() / 3600

                    # Timeout dynamique depuis UMAState si disponible
                    timeout_h = MAX_ORACLE_WAIT_HOURS
                    if uma_state is not None:
                        timeout_h = uma_state.dynamic_timeout_hours

                    if elapsed_h > timeout_h:
                        phase_label = uma_state.phase.value if uma_state else "UNKNOWN"
                        msg = (
                            f"⚠️ *Oracle timeout* `{condition_id[:16]}`\n"
                            f"Phase UMA: `{phase_label}`\n"
                            f"{elapsed_h:.1f}h écoulées (seuil={timeout_h:.1f}h)\n"
                            f"_{question}_"
                        )
                        log.error(
                            f"Oracle timeout [{condition_id[:16]}] "
                            f"phase={phase_label} "
                            f"elapsed={elapsed_h:.1f}h timeout={timeout_h:.1f}h"
                        )
                        _telegram(msg)
                except Exception:
                    pass

            phase_label = uma_state.human_summary() if uma_state else "phase=UNKNOWN"
            log.info(f"En attente oracle [{phase_label}] : {question}…")
            continue

        # ── 8. Terminal → ferme la position ───────────────────────────────────
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
    """
    Calcule le PnL, ferme la position et notifie.

    Si le statut est RESOLVED_YES/NO et que Gamma a retourné des données,
    on calcule le PnL depuis les prix Gamma (rapide, fiable en général).

    Si Gamma est indisponible (data = {}) mais que le marché est résolu
    on-chain (is_condition_resolved), on déclenche redeem_position() pour
    récupérer les USDC directement via le contrat CTF — sans aucune
    dépendance à Polymarket Labs.
    """
    from config.settings import DRY_RUN as GLOBAL_DRY_RUN

    res_price = _resolution_price(status, side, data)

    # ── Tentative de redeem on-chain si Gamma indisponible ────────────────────
    # Cas : Gamma retourne {} mais on-chain le marché est résolu.
    # On appelle redeemPositions() pour récupérer les USDC sans passer par l'API.
    redeem_attempted = False
    if not data and status in (MarketStatus.RESOLVED_YES, MarketStatus.RESOLVED_NO):
        if is_condition_resolved(condition_id):
            log.info(
                f"Gamma indisponible mais marché résolu on-chain → redeem CTF | "
                f"{condition_id[:16]} | side={side}"
            )
            redeem_result = redeem_position(
                condition_id = condition_id,
                side         = side,
                dry_run      = dry_run or GLOBAL_DRY_RUN,
            )
            redeem_attempted = True
            log_redeem(
                condition_id = condition_id,
                side         = side,
                result       = redeem_result.value,
                dry_run      = dry_run or GLOBAL_DRY_RUN,
            )
            if redeem_result == RedeemResult.SUCCESS:
                log.info(f"✅ Redeem CTF on-chain réussi | {condition_id[:16]}")
            elif redeem_result == RedeemResult.DRY_RUN:
                log.info(f"[DRY-RUN] Redeem CTF simulé | {condition_id[:16]}")
            else:
                log.warning(
                    f"⚠️ Redeem CTF échoué ({redeem_result.value}) — "
                    f"PnL calculé depuis prix estimé | {condition_id[:16]}"
                )

    # ── Calcul PnL ────────────────────────────────────────────────────────────
    # PnL = (prix_résolution − prix_entrée) × nombre_de_tokens
    nb_tokens = size_usd / price_entry if price_entry > 0 else 0
    pnl       = round((res_price - price_entry) * nb_tokens, 4)

    result   = "win" if pnl >= 0 else "loss"
    emoji    = "✅" if pnl >= 0 else "❌"
    question = pos.get("question", "")[:50]

    redeem_note = " [redeem on-chain]" if redeem_attempted else ""
    log.info(
        f"{emoji} Fermé [{side}] {status.value}{redeem_note} | "
        f"PnL={pnl:+.2f}$ ({price_entry:.3f}→{res_price:.3f}) | {question}…"
    )

    close_position(condition_id, result, pnl)

    # ── Mise à jour du feature store avec l'outcome réel ────────────────────
    # Permet d'entraîner des modèles ML sur les données historiques du bot.
    try:
        from signals.feature_store import update_outcomes
        n = update_outcomes(
            condition_id   = condition_id,
            outcome_yes    = (status == MarketStatus.RESOLVED_YES),
            observed_value = None,   # déjà dans onchain_ops si disponible
            pnl_usd        = pnl,
        )
        if n > 0:
            log.debug(f"Feature store : {n} prédiction(s) labellisée(s) [{condition_id[:16]}]")
    except Exception:
        pass   # non-bloquant

    # Notification Telegram pour CANCELLED, pertes importantes, ou redeem on-chain
    if status == MarketStatus.CANCELLED or pnl < -10 or redeem_attempted:
        msg_parts = [
            f"{emoji} *Position fermée* `{condition_id[:16]}`",
            f"Statut: `{status.value}`",
            f"PnL: `{pnl:+.2f}$`",
            f"_{question}_",
        ]
        if status == MarketStatus.CANCELLED:
            msg_parts.append(f"⚠️ Remboursement partiel à {res_price:.3f}$")
        if redeem_attempted:
            msg_parts.append(f"🔗 Redeem CTF on-chain déclenché (Gamma indisponible)")
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