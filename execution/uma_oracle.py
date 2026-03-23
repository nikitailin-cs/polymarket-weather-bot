"""
execution/uma_oracle.py
-----------------------
Surveillance on-chain de l'Optimistic Oracle UMA v2 sur Polygon.

Séquence temporelle contractuelle exacte :
  ┌─────────────────────────────────────────────────────────────────┐
  │  t=0   Marché fermé (end_date atteinte)                         │
  │  t=?   AWAITING_PROPOSAL : fenêtre ouverte, aucune proposition  │
  │         → n'importe qui peut soumettre résultat + bond USDC      │
  │         → bond confisqué si proposition frauduleuse              │
  │  t=P   ProposePrice on-chain                                     │
  │  t=P+L IN_DISPUTE_WINDOW : liveness = 7200s (2h) par défaut     │
  │         → n'importe qui peut disputer en soumettant un bond      │
  │  t=P+L SETTLEABLE : settle() appelable si pas de dispute         │
  │         → settle() déclenche le transfert USDC aux holders        │
  │  t=P+L SETTLED : résolution finale, redeemPositions() disponible │
  │                                                                   │
  │  Si dispute avant t=P+L :                                        │
  │  DISPUTED → vote UMA token holders → 5-7 jours → résolution forcée│
  └─────────────────────────────────────────────────────────────────┘

Ce module modélise chaque phase avec ses timestamps exacts pour permettre
au bot d'adapter son comportement (throttle poll, alertes, timeout) à la
réalité contractuelle plutôt qu'à des constantes arbitraires.
"""
from __future__ import annotations

import os, time, logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# ── Contrat UMA OptimisticOracleV2 Polygon mainnet ───────────────────────────
UMA_OO_ADDRESS         = "0xee3Afe347D5C74317041E2618C49534dAf887c24"
POLYMARKET_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Liveness par défaut UMA pour Polymarket = 7200s (2h)
# Peut être surchargé par customLiveness dans getRequest()
DEFAULT_LIVENESS_SEC = 7200

# Délai max attendu pour qu'un proposer soumette après fermeture du marché
# En pratique Polymarket soumet en < 1h, mais on tolère jusqu'à 6h
MAX_PROPOSAL_WAIT_SEC = 6 * 3600

# RPC Polygon
POLYGON_RPC_URLS = [
    os.getenv("POLYGON_RPC_URL", ""),
    "https://polygon-rpc.com",
    "https://rpc-mainnet.matic.network",
    "https://matic-mainnet.chainstacklabs.com",
]

UMA_ABI = [
    {
        "name": "getRequest",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "requester",    "type": "address"},
            {"name": "identifier",   "type": "bytes32"},
            {"name": "timestamp",    "type": "uint256"},
            {"name": "ancillaryData","type": "bytes"},
        ],
        "outputs": [{
            "name": "", "type": "tuple",
            "components": [
                {"name": "proposer",       "type": "address"},
                {"name": "disputer",       "type": "address"},
                {"name": "currency",       "type": "address"},
                {"name": "settled",        "type": "bool"},
                {"name": "proposedPrice",  "type": "int256"},
                {"name": "resolvedPrice",  "type": "int256"},
                {"name": "expirationTime", "type": "uint256"},
                {"name": "reward",         "type": "uint256"},
                {"name": "finalFee",       "type": "uint256"},
                {"name": "bond",           "type": "uint256"},
                {"name": "customLiveness", "type": "uint256"},
            ],
        }],
    },
    {"name": "ProposePrice",  "type": "event", "inputs": [
        {"name": "requester",     "type": "address", "indexed": True},
        {"name": "identifier",    "type": "bytes32", "indexed": True},
        {"name": "timestamp",     "type": "uint256", "indexed": False},
        {"name": "ancillaryData", "type": "bytes",   "indexed": False},
        {"name": "proposer",      "type": "address", "indexed": False},
        {"name": "proposedPrice", "type": "int256",  "indexed": False},
        {"name": "expirationTime","type": "uint256", "indexed": False},
    ]},
    {"name": "DisputePrice", "type": "event", "inputs": [
        {"name": "requester",     "type": "address", "indexed": True},
        {"name": "identifier",    "type": "bytes32", "indexed": True},
        {"name": "timestamp",     "type": "uint256", "indexed": False},
        {"name": "ancillaryData", "type": "bytes",   "indexed": False},
        {"name": "proposer",      "type": "address", "indexed": False},
        {"name": "disputer",      "type": "address", "indexed": False},
        {"name": "proposedPrice", "type": "int256",  "indexed": False},
    ]},
    {"name": "Settle", "type": "event", "inputs": [
        {"name": "requester",     "type": "address", "indexed": True},
        {"name": "identifier",    "type": "bytes32", "indexed": True},
        {"name": "timestamp",     "type": "uint256", "indexed": False},
        {"name": "ancillaryData", "type": "bytes",   "indexed": False},
        {"name": "proposer",      "type": "address", "indexed": False},
        {"name": "disputer",      "type": "address", "indexed": False},
        {"name": "price",         "type": "int256",  "indexed": False},
        {"name": "payout",        "type": "uint256", "indexed": False},
    ]},
]


# ── Enum phases UMA — séquence temporelle stricte ────────────────────────────

class UMAPhase(str, Enum):
    """
    Modélise chaque phase de la séquence de résolution UMA.
    L'ordre est strictement croissant — on ne peut pas régresser.
    """
    AWAITING_PROPOSAL  = "AWAITING_PROPOSAL"   # Marché fermé, aucune proposition
    IN_DISPUTE_WINDOW  = "IN_DISPUTE_WINDOW"   # Proposé, fenêtre dispute en cours
    SETTLEABLE         = "SETTLEABLE"           # Fenêtre expirée, settle() appelable
    SETTLED            = "SETTLED"              # settle() exécuté, résolution finale
    DISPUTED           = "DISPUTED"             # Dispute déclenchée, vote UMA en cours
    UNKNOWN            = "UNKNOWN"              # RPC indisponible


# Rétrocompatibilité — l'ancien UMAStatus reste disponible
class UMAStatus(str, Enum):
    PENDING   = "PENDING"
    PROPOSED  = "PROPOSED"
    DISPUTED  = "DISPUTED"
    SETTLED   = "SETTLED"
    UNKNOWN   = "UNKNOWN"


@dataclass
class UMAState:
    """
    État complet d'un marché dans le pipeline UMA.
    Contient tous les timestamps nécessaires pour calculer
    dynamiquement les délais et throttles.
    """
    condition_id:     str
    phase:            UMAPhase        = UMAPhase.UNKNOWN

    # Timestamps Unix (0 = inconnu)
    market_closed_at: int             = 0   # end_date du marché
    proposed_at:      int             = 0   # timestamp ProposePrice
    dispute_window_ends: int          = 0   # proposed_at + liveness
    settled_at:       int             = 0   # timestamp Settle

    # Détails proposition
    proposer:         str             = ""
    proposed_price:   Optional[int]   = None   # int256 (1e18 = YES, 0 = NO)
    liveness_sec:     int             = DEFAULT_LIVENESS_SEC
    bond_usdc:        float           = 0.0

    # Détails dispute (si applicable)
    disputer:         str             = ""
    dispute_price:    Optional[int]   = None

    # Prix résolution finale
    resolved_price:   Optional[int]   = None   # int256

    # Méta
    fetched_at:       int             = field(default_factory=lambda: int(time.time()))
    rpc_available:    bool            = True

    # ── Propriétés calculées ──────────────────────────────────────────────────

    @property
    def seconds_since_closed(self) -> Optional[int]:
        if not self.market_closed_at:
            return None
        return int(time.time()) - self.market_closed_at

    @property
    def dispute_window_remaining_sec(self) -> Optional[int]:
        """Secondes restantes dans la fenêtre de dispute. 0 si expirée."""
        if not self.dispute_window_ends:
            return None
        remaining = self.dispute_window_ends - int(time.time())
        return max(0, remaining)

    @property
    def is_settleable(self) -> bool:
        """True si settle() peut être appelé maintenant."""
        if self.phase == UMAPhase.IN_DISPUTE_WINDOW:
            return self.dispute_window_remaining_sec == 0
        return self.phase == UMAPhase.SETTLEABLE

    @property
    def recommended_poll_interval_sec(self) -> int:
        """
        Intervalle de poll recommandé selon la phase UMA.
        Remplace MAX_ORACLE_WAIT_HOURS fixe par un délai adaptatif.
        """
        if self.phase == UMAPhase.IN_DISPUTE_WINDOW:
            remaining = self.dispute_window_remaining_sec or DEFAULT_LIVENESS_SEC
            # Poll toutes les 5 min pendant la fenêtre de dispute
            # Plus souvent si on approche de l'expiration
            if remaining < 600:    # < 10 min
                return 60          # toutes les 1 min
            if remaining < 1800:   # < 30 min
                return 300         # toutes les 5 min
            return 600             # toutes les 10 min

        if self.phase == UMAPhase.AWAITING_PROPOSAL:
            # Proposition attendue — poll toutes les 30 min
            # En pratique Polymarket propose en < 30min post-fermeture
            return 1800

        if self.phase == UMAPhase.SETTLEABLE:
            # settle() appelable — poll fréquent (Gamma se met à jour vite ici)
            return 300

        if self.phase == UMAPhase.DISPUTED:
            # Vote UMA = 5-7 jours — inutile de poller souvent
            return 4 * 3600   # toutes les 4h

        return 1800   # défaut

    @property
    def dynamic_timeout_hours(self) -> float:
        """
        Timeout oracle dynamique basé sur la phase UMA réelle.
        Remplace MAX_ORACLE_WAIT_HOURS=48h fixe.
        """
        if self.phase == UMAPhase.AWAITING_PROPOSAL:
            # Si pas de proposition après 6h → anomalie
            return MAX_PROPOSAL_WAIT_SEC / 3600

        if self.phase == UMAPhase.IN_DISPUTE_WINDOW:
            # Résolution attendue dans liveness + 1h de marge
            return (self.liveness_sec + 3600) / 3600

        if self.phase == UMAPhase.SETTLEABLE:
            # settle() devrait être appelé rapidement (bots de settlement)
            return 2.0

        if self.phase == UMAPhase.DISPUTED:
            # Vote UMA = jusqu'à 7 jours
            return 7 * 24

        return 48.0   # défaut conservateur

    @property
    def proposed_yes(self) -> Optional[bool]:
        """True si la proposition est YES (price = 1e18), False si NO (price = 0)."""
        if self.proposed_price is None:
            return None
        # UMA encode YES = 1000000000000000000 (1e18), NO = 0
        return self.proposed_price > 0

    def human_summary(self) -> str:
        """Résumé lisible de l'état UMA pour les logs."""
        now = int(time.time())
        parts = [f"phase={self.phase.value}"]

        if self.phase == UMAPhase.AWAITING_PROPOSAL:
            if self.market_closed_at:
                waited = (now - self.market_closed_at) // 60
                parts.append(f"fermé_il_y_a={waited}min")

        elif self.phase == UMAPhase.IN_DISPUTE_WINDOW:
            rem = self.dispute_window_remaining_sec
            parts.append(f"dispute_dans={rem//60}min" if rem else "fenêtre_expirée")
            if self.proposed_yes is not None:
                parts.append(f"proposition={'YES' if self.proposed_yes else 'NO'}")

        elif self.phase == UMAPhase.SETTLEABLE:
            parts.append("settle()_appelable")

        elif self.phase == UMAPhase.DISPUTED:
            parts.append(f"disputer={self.disputer[:10] if self.disputer else '?'}")

        elif self.phase == UMAPhase.SETTLED:
            if self.resolved_price is not None:
                parts.append(f"résolu={'YES' if self.resolved_price > 0 else 'NO'}")

        return " | ".join(parts)


# ── Cache ─────────────────────────────────────────────────────────────────────

_state_cache: dict[str, UMAState] = {}


def _cache_ttl(state: UMAState) -> int:
    """TTL du cache selon la phase — évite les appels RPC inutiles."""
    if state.phase == UMAPhase.SETTLED:
        return 86400        # terminal — 24h
    if state.phase == UMAPhase.DISPUTED:
        return 4 * 3600     # stable — 4h
    if state.phase == UMAPhase.IN_DISPUTE_WINDOW:
        rem = state.dispute_window_remaining_sec or DEFAULT_LIVENESS_SEC
        return min(120, rem // 2)   # 2 min max, ou moitié du temps restant
    return 120   # défaut 2 min


# ── Connexion Web3 ────────────────────────────────────────────────────────────

def _get_web3():
    try:
        from web3 import Web3
    except ImportError:
        log.warning("web3 non installé — pip install web3")
        return None
    for url in POLYGON_RPC_URLS:
        if not url:
            continue
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    log.warning("UMA Oracle: aucun RPC Polygon disponible")
    return None


def _matches_condition(ancillary_data: bytes, condition_id: str) -> bool:
    """Vérifie si un event UMA correspond au condition_id Polymarket."""
    if not isinstance(ancillary_data, bytes):
        return False
    cid_hex = condition_id.lower().replace("0x", "")
    return cid_hex in ancillary_data.hex()


# ── API principale ────────────────────────────────────────────────────────────

def get_uma_state(
    condition_id:     str,
    market_closed_at: int = 0,   # timestamp Unix de fermeture du marché
) -> UMAState:
    """
    Retourne l'UMAState complet d'un marché, avec tous les timestamps
    nécessaires pour calculer dynamiquement les délais de poll et timeout.

    Séquence de détection (priorité décroissante) :
      1. Event Settle     → SETTLED
      2. Event DisputePrice → DISPUTED
      3. Event ProposePrice → IN_DISPUTE_WINDOW ou SETTLEABLE
      4. Aucun event       → AWAITING_PROPOSAL

    Cache adaptatif selon la phase (2min pendant dispute_window, 4h si DISPUTED).
    """
    now_mono = time.monotonic()
    now_unix = int(time.time())

    # ── Cache ──────────────────────────────────────────────────────────────────
    if condition_id in _state_cache:
        cached = _state_cache[condition_id]
        age    = now_unix - cached.fetched_at
        ttl    = _cache_ttl(cached)
        if age < ttl:
            return cached
        if cached.phase == UMAPhase.SETTLED:
            return cached   # terminal

    w3 = _get_web3()
    if w3 is None:
        state = UMAState(
            condition_id=condition_id,
            phase=UMAPhase.UNKNOWN,
            market_closed_at=market_closed_at,
            rpc_available=False,
        )
        _state_cache[condition_id] = state
        return state

    try:
        from web3 import Web3
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(UMA_OO_ADDRESS),
            abi=UMA_ABI,
        )

        # Fenêtre de scan : 10 jours (~432 000 blocs Polygon à ~2s/bloc)
        latest_block = w3.eth.block_number
        from_block   = max(0, latest_block - 432_000)

        requester_filter = {"requester": Web3.to_checksum_address(POLYMARKET_CTF_ADDRESS)}

        # ── 1. Settle ─────────────────────────────────────────────────────────
        try:
            for evt in contract.events.Settle.create_filter(
                from_block=from_block, to_block="latest",
                argument_filters=requester_filter,
            ).get_all_entries():
                if _matches_condition(evt["args"].get("ancillaryData", b""), condition_id):
                    price = evt["args"].get("price", 0)
                    state = UMAState(
                        condition_id   = condition_id,
                        phase          = UMAPhase.SETTLED,
                        market_closed_at = market_closed_at,
                        resolved_price = price,
                        settled_at     = int(time.time()),
                    )
                    _state_cache[condition_id] = state
                    log.info(
                        f"UMA SETTLED [{condition_id[:16]}] "
                        f"→ {'YES' if price > 0 else 'NO'}"
                    )
                    return state
        except Exception as e:
            log.debug(f"UMA Settle scan: {e}")

        # ── 2. DisputePrice ───────────────────────────────────────────────────
        try:
            for evt in contract.events.DisputePrice.create_filter(
                from_block=from_block, to_block="latest",
                argument_filters=requester_filter,
            ).get_all_entries():
                if _matches_condition(evt["args"].get("ancillaryData", b""), condition_id):
                    state = UMAState(
                        condition_id   = condition_id,
                        phase          = UMAPhase.DISPUTED,
                        market_closed_at = market_closed_at,
                        proposer       = evt["args"].get("proposer", ""),
                        disputer       = evt["args"].get("disputer", ""),
                        proposed_price = evt["args"].get("proposedPrice"),
                    )
                    _state_cache[condition_id] = state
                    log.warning(
                        f"UMA DISPUTED [{condition_id[:16]}] "
                        f"proposer={state.proposer[:10]} "
                        f"disputer={state.disputer[:10]}"
                    )
                    return state
        except Exception as e:
            log.debug(f"UMA DisputePrice scan: {e}")

        # ── 3. ProposePrice ───────────────────────────────────────────────────
        try:
            proposals = contract.events.ProposePrice.create_filter(
                from_block=from_block, to_block="latest",
                argument_filters=requester_filter,
            ).get_all_entries()

            for evt in proposals:
                if _matches_condition(evt["args"].get("ancillaryData", b""), condition_id):
                    exp_time       = evt["args"].get("expirationTime", 0)
                    proposed_price = evt["args"].get("proposedPrice")
                    proposer       = evt["args"].get("proposer", "")
                    ancillary_data = evt["args"].get("ancillaryData", b"")
                    identifier     = evt["args"].get("identifier", b"\x00" * 32)
                    req_timestamp  = evt["args"].get("timestamp", 0)

                    # ── Lire customLiveness réel via getRequest() ─────────────
                    # "inaltérable de deux heures" est vrai pour Polymarket en
                    # pratique, mais contractuellement customLiveness peut varier.
                    # On lit la valeur exacte on-chain pour être précis.
                    liveness = DEFAULT_LIVENESS_SEC
                    bond_usdc = 0.0
                    try:
                        req = contract.functions.getRequest(
                            Web3.to_checksum_address(POLYMARKET_CTF_ADDRESS),
                            identifier,
                            req_timestamp,
                            ancillary_data,
                        ).call()
                        # req = tuple : proposer, disputer, currency, settled,
                        #               proposedPrice, resolvedPrice, expirationTime,
                        #               reward, finalFee, bond, customLiveness
                        custom_liveness = req[10]   # index 10 = customLiveness
                        bond_wei        = req[9]    # index 9  = bond
                        if custom_liveness > 0:
                            liveness = int(custom_liveness)
                            log.debug(
                                f"customLiveness [{condition_id[:16]}]: "
                                f"{liveness}s (≠ défaut {DEFAULT_LIVENESS_SEC}s)"
                                if liveness != DEFAULT_LIVENESS_SEC else
                                f"customLiveness [{condition_id[:16]}]: {liveness}s (= défaut)"
                            )
                        bond_usdc = bond_wei / 1e6
                    except Exception as e:
                        log.debug(f"getRequest() erreur [{condition_id[:16]}]: {e}")

                    # Recalcule dispute_window_ends depuis liveness réel
                    # expirationTime = proposed_at + liveness (set par le contrat)
                    # On utilise expirationTime de l'event (source de vérité)
                    # et on déduit proposed_at = expirationTime - liveness_réel
                    proposed_at = exp_time - liveness

                    if now_unix < exp_time:
                        phase = UMAPhase.IN_DISPUTE_WINDOW
                    else:
                        phase = UMAPhase.SETTLEABLE

                    state = UMAState(
                        condition_id        = condition_id,
                        phase               = phase,
                        market_closed_at    = market_closed_at,
                        proposed_at         = proposed_at,
                        dispute_window_ends = exp_time,
                        proposer            = proposer,
                        proposed_price      = proposed_price,
                        liveness_sec        = liveness,
                        bond_usdc           = bond_usdc,
                    )
                    _state_cache[condition_id] = state
                    log.info(
                        f"UMA {phase.value} [{condition_id[:16]}] "
                        f"{state.human_summary()} "
                        f"liveness={liveness}s bond={bond_usdc:.0f}USDC"
                    )
                    return state
        except Exception as e:
            log.debug(f"UMA ProposePrice scan: {e}")

        # ── 4. Aucun event → AWAITING_PROPOSAL ───────────────────────────────
        state = UMAState(
            condition_id   = condition_id,
            phase          = UMAPhase.AWAITING_PROPOSAL,
            market_closed_at = market_closed_at,
        )
        _state_cache[condition_id] = state
        log.debug(
            f"UMA AWAITING_PROPOSAL [{condition_id[:16]}] "
            f"— fermé il y a {(now_unix - market_closed_at)//60}min"
            if market_closed_at else
            f"UMA AWAITING_PROPOSAL [{condition_id[:16]}]"
        )
        return state

    except Exception as e:
        log.debug(f"UMA get_uma_state erreur [{condition_id[:16]}]: {e}")
        state = UMAState(
            condition_id=condition_id,
            phase=UMAPhase.UNKNOWN,
            market_closed_at=market_closed_at,
            rpc_available=False,
        )
        _state_cache[condition_id] = state
        return state


# ── Rétrocompatibilité ────────────────────────────────────────────────────────

def get_uma_status(condition_id: str, end_timestamp: Optional[int] = None) -> UMAStatus:
    """
    Interface rétrocompatible avec l'ancien UMAStatus enum.
    Utilise get_uma_state() en interne.
    """
    state = get_uma_state(condition_id, market_closed_at=end_timestamp or 0)
    mapping = {
        UMAPhase.AWAITING_PROPOSAL: UMAStatus.PENDING,
        UMAPhase.IN_DISPUTE_WINDOW: UMAStatus.PROPOSED,
        UMAPhase.SETTLEABLE:        UMAStatus.PROPOSED,
        UMAPhase.SETTLED:           UMAStatus.SETTLED,
        UMAPhase.DISPUTED:          UMAStatus.DISPUTED,
        UMAPhase.UNKNOWN:           UMAStatus.UNKNOWN,
    }
    return mapping.get(state.phase, UMAStatus.UNKNOWN)


def get_dispute_time_remaining(condition_id: str) -> Optional[int]:
    """Rétrocompatibilité — secondes restantes dans la fenêtre de dispute."""
    state = get_uma_state(condition_id)
    return state.dispute_window_remaining_sec


if __name__ == "__main__":
    import sys
    cid = sys.argv[1] if len(sys.argv) > 1 else "0x113e93e6c20e4340afedd0d405648d"
    closed_ts = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    print(f"\n🔗 UMA Oracle — séquence temporelle complète")
    print(f"   Contrat : {UMA_OO_ADDRESS}")
    print(f"   Marché  : {cid}")

    state = get_uma_state(cid, market_closed_at=closed_ts)
    print(f"\n   Phase    : {state.phase.value}")
    print(f"   Résumé   : {state.human_summary()}")
    print(f"   Poll recommandé : {state.recommended_poll_interval_sec}s")
    print(f"   Timeout dynamique : {state.dynamic_timeout_hours:.1f}h")
    if state.dispute_window_remaining_sec is not None:
        rem = state.dispute_window_remaining_sec
        print(f"   Fenêtre dispute : {rem//3600}h{(rem%3600)//60}min restantes")
    if state.proposed_yes is not None:
        print(f"   Proposition : {'YES' if state.proposed_yes else 'NO'}")