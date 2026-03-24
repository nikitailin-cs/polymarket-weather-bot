"""
execution/uma_disputer.py
--------------------------
Participation active au mécanisme de dispute UMA OptimisticOracleV2.

Quand le bot détecte une proposition erronée pendant IN_DISPUTE_WINDOW
(proposed_price UMA ≠ ContractSpec.evaluate(observed_temp)), il peut
déclencher une dispute on-chain via disputePrice().

Mécanique contractuelle :
  1. Le bot vérifie que proposed_price UMA diverge de sa prévision météo
  2. Il approuve le contrat UMA à dépenser son bond USDC (approve())
  3. Il appelle disputePrice() sur l'OO — le bond est prélevé immédiatement
  4. Si le DVM vote en sa faveur → bond du proposer confisqué + bond rendu
  5. Si le DVM vote contre → bond du bot confisqué

Risque : le bot perd son bond (500 USDC) si le DVM ne valide pas la dispute.
Seuil de confiance minimum : MIN_DISPUTE_CONFIDENCE (défaut 0.90).

Contrats :
  OO v2   : 0xee3Afe347D5C74317041E2618C49534dAf887c24
  USDC    : 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
  requester (Polymarket CTF Exchange) : 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
"""
from __future__ import annotations

import os, time, logging

from config.settings import resolve_trading_private_key
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ── Adresses ─────────────────────────────────────────────────────────────────
UMA_OO_ADDRESS         = "0xee3Afe347D5C74317041E2618C49534dAf887c24"
USDC_ADDRESS           = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYMARKET_CTF_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# Bond UMA pour Polymarket = 500 USDC (documenté, lu depuis getRequest() en pratique)
# Le bot doit disposer de ce montant + finalFee (~50 USDC) pour disputer
DEFAULT_BOND_USDC = float(os.getenv("UMA_DISPUTE_BOND_USDC", "500.0"))

# Confiance minimale requise pour déclencher une dispute
# 0.90 = le bot est sûr à 90%+ que la proposition est erronée
MIN_DISPUTE_CONFIDENCE = float(os.getenv("UMA_DISPUTE_MIN_CONFIDENCE", "0.90"))

# Activer/désactiver les disputes on-chain (désactivé par défaut — risque financier)
DISPUTE_ENABLED = os.getenv("UMA_DISPUTE_ENABLED", "0") == "1"

# ABI OO v2 — disputePrice + getRequest
UMA_DISPUTE_ABI = [
    {
        "name": "disputePrice",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "requester",    "type": "address"},
            {"name": "identifier",   "type": "bytes32"},
            {"name": "timestamp",    "type": "uint256"},
            {"name": "ancillaryData","type": "bytes"},
        ],
        "outputs": [],
    },
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
]

# ABI USDC — approve + allowance
USDC_ABI = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [{"name": "account", "type": "address"}],
        "outputs": [{"name": "",        "type": "uint256"}],
    },
]

# RPC Polygon
POLYGON_RPC_URLS = [
    os.getenv("POLYGON_RPC_URL", ""),
    "https://polygon-rpc.com",
    "https://rpc-mainnet.matic.network",
]


class DisputeResult(str, Enum):
    DISPUTED        = "DISPUTED"         # disputePrice() confirmé on-chain
    NO_DIVERGENCE   = "NO_DIVERGENCE"    # proposed_price correspond à evaluate()
    LOW_CONFIDENCE  = "LOW_CONFIDENCE"   # confiance insuffisante pour disputer
    WINDOW_EXPIRED  = "WINDOW_EXPIRED"   # fenêtre de dispute déjà fermée
    INSUFFICIENT_USDC = "INSUFFICIENT_USDC"  # solde USDC insuffisant pour le bond
    DISABLED        = "DISABLED"         # UMA_DISPUTE_ENABLED=0
    DRY_RUN         = "DRY_RUN"          # simulation
    FAILED          = "FAILED"           # erreur tx ou RPC


@dataclass
class DisputeAssessment:
    """
    Résultat de l'évaluation d'une proposition UMA contre les données météo.
    """
    condition_id:       str
    proposed_yes:       Optional[bool]   # ce que l'UMA a proposé
    contract_says_yes:  Optional[bool]   # ce que evaluate() dit
    diverges:           bool             # True si conflit
    observed_temp:      Optional[float]  # température réellement observée
    threshold_value:    Optional[float]
    threshold_op:       Optional[str]
    confidence:         float            # confiance dans notre évaluation (0-1)
    bond_required_usdc: float
    should_dispute:     bool             # True si on doit disputer


def _get_web3():
    try:
        from web3 import Web3
    except ImportError:
        return None
    for url in POLYGON_RPC_URLS:
        if not url:
            continue
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    return None


def _get_wallet():
    """Retourne (address, private_key) depuis PRIVATE_KEY ou POLYMARKET_PKEY."""
    pk = resolve_trading_private_key() or ""
    if not pk:
        return None, None
    try:
        from eth_account import Account
        acct = Account.from_key(pk)
        return acct.address, pk
    except Exception:
        return None, None


def _ensure_usdc_allowance(
    w3,
    wallet_address: str,
    private_key:    str,
    amount_usdc:    float,
    gas_price_gwei: float = 100.0,
) -> bool:
    """
    Vérifie et approuve l'allowance USDC pour le contrat UMA OO.
    Retourne True si l'allowance est suffisante (après approve si nécessaire).
    """
    try:
        from web3 import Web3
        usdc = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS),
            abi=USDC_ABI,
        )
        amount_wei = int(amount_usdc * 1e6)
        oo_address = Web3.to_checksum_address(UMA_OO_ADDRESS)

        # Vérifie l'allowance actuelle
        current_allowance = usdc.functions.allowance(
            Web3.to_checksum_address(wallet_address),
            oo_address,
        ).call()

        if current_allowance >= amount_wei:
            log.debug(f"USDC allowance suffisante: {current_allowance/1e6:.2f} USDC")
            return True

        # Approve le montant nécessaire + marge (2× pour éviter re-approve)
        approve_amount = amount_wei * 2
        log.info(
            f"Approve USDC → OO | montant={approve_amount/1e6:.2f} USDC | "
            f"wallet={wallet_address[:12]}..."
        )

        nonce     = w3.eth.get_transaction_count(Web3.to_checksum_address(wallet_address))
        gas_price = w3.to_wei(gas_price_gwei, "gwei")

        tx = usdc.functions.approve(
            oo_address,
            approve_amount,
        ).build_transaction({
            "from":     Web3.to_checksum_address(wallet_address),
            "nonce":    nonce,
            "gasPrice": gas_price,
            "gas":      80_000,
            "chainId":  137,
        })

        from eth_account import Account
        signed  = Account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] == 1:
            log.info(f"✅ USDC approve confirmé | tx={tx_hash.hex()[:20]}...")
            return True
        else:
            log.error(f"❌ USDC approve échouée | tx={tx_hash.hex()[:20]}...")
            return False

    except Exception as e:
        log.error(f"_ensure_usdc_allowance erreur: {e}")
        return False


def assess_proposal(
    condition_id: str,
    uma_state,           # UMAState depuis uma_oracle
    spec_cache,          # ContractSpecCache
) -> DisputeAssessment:
    """
    Compare proposed_price UMA avec ContractSpec.evaluate(observed_temp).

    Retourne une DisputeAssessment indiquant si une dispute est justifiée.
    N'exécute aucune transaction — évaluation pure.
    """
    # Valeurs par défaut
    assessment = DisputeAssessment(
        condition_id       = condition_id,
        proposed_yes       = uma_state.proposed_yes,
        contract_says_yes  = None,
        diverges           = False,
        observed_temp      = None,
        threshold_value    = None,
        threshold_op       = None,
        confidence         = 0.0,
        bond_required_usdc = uma_state.bond_usdc or DEFAULT_BOND_USDC,
        should_dispute     = False,
    )

    if uma_state.proposed_yes is None:
        return assessment   # pas de proposition à évaluer

    # Récupère le ContractSpec
    if spec_cache is None:
        return assessment
    spec = spec_cache.get(condition_id)
    if spec is None or spec.weather_variable not in ("temp_max", "temp_min", "temp_avg"):
        return assessment

    assessment.threshold_value = spec.threshold_value
    assessment.threshold_op    = spec.threshold_op

    # Récupère la température observée
    city     = spec.city
    end_date = spec.end_date_iso[:10] if spec.end_date_iso else ""
    if not city or not end_date:
        return assessment

    try:
        from signals.sources.open_meteo import get_max_temp, get_min_temp
        if spec.weather_variable == "temp_max":
            observed = get_max_temp(city, end_date)
        elif spec.weather_variable == "temp_min":
            observed = get_min_temp(city, end_date)
        else:
            observed = get_max_temp(city, end_date)

        if observed is None:
            return assessment

        assessment.observed_temp = observed

        # Applique la condition contractuelle
        contract_says_yes        = spec.evaluate(observed)
        assessment.contract_says_yes = contract_says_yes

        # Divergence détectée ?
        assessment.diverges = (contract_says_yes != uma_state.proposed_yes)

        if not assessment.diverges:
            return assessment

        # Calcule la confiance dans notre évaluation
        # Plus la température observée est loin du seuil, plus on est sûrs
        margin = abs(observed - spec.threshold_value)
        # sigma typique = 1.5°C — confiance proportionnelle à la marge
        sigma  = 1.5
        import math
        confidence = min(0.99, 0.5 + 0.5 * math.erf(margin / (sigma * math.sqrt(2))))
        assessment.confidence = round(confidence, 3)

        assessment.should_dispute = (
            assessment.diverges
            and assessment.confidence >= MIN_DISPUTE_CONFIDENCE
        )

        log.info(
            f"DisputeAssessment [{condition_id[:16]}] | "
            f"UMA={'YES' if uma_state.proposed_yes else 'NO'} | "
            f"contrat={'YES' if contract_says_yes else 'NO'} | "
            f"observe={observed:.1f}°C seuil={spec.threshold_op} "
            f"{spec.threshold_value}°C | "
            f"marge={margin:.1f}°C conf={confidence:.0%} | "
            f"dispute={'OUI' if assessment.should_dispute else 'NON (conf insuffisante)'}"
        )

    except Exception as e:
        log.debug(f"assess_proposal erreur [{condition_id[:16]}]: {e}")

    return assessment


def execute_dispute(
    condition_id:   str,
    uma_state,          # UMAState
    assessment:     DisputeAssessment,
    dry_run:        bool = True,
    gas_price_gwei: float = 120.0,
) -> DisputeResult:
    """
    Exécute disputePrice() on-chain si toutes les conditions sont réunies.

    Pré-conditions vérifiées avant toute tx :
      1. DISPUTE_ENABLED=1 (désactivé par défaut)
      2. Fenêtre de dispute encore ouverte
      3. Divergence confirmée avec confiance >= MIN_DISPUTE_CONFIDENCE
      4. Solde USDC >= bond_required + finalFee
      5. Allowance USDC suffisante (approve() si nécessaire)
    """
    from monitoring.onchain_logger import log_resolution_check

    # Log assessment dans onchain_ops
    log_resolution_check(
        condition_id               = condition_id,
        source                     = "dispute_assessment",
        resolved                   = False,
        resolution_matches_contract= not assessment.diverges,
        observed_value             = assessment.observed_temp,
        contract_threshold         = assessment.threshold_value,
        contract_op                = assessment.threshold_op,
        gamma_status               = None,
        uma_status                 = uma_state.phase.value,
        conflict                   = assessment.diverges,
    )

    if dry_run:
        if assessment.should_dispute:
            log.info(
                f"[DRY-RUN] disputePrice() serait déclenché | "
                f"{condition_id[:16]} | "
                f"bond={assessment.bond_required_usdc:.0f} USDC | "
                f"conf={assessment.confidence:.0%}"
            )
        return DisputeResult.DRY_RUN

    if not DISPUTE_ENABLED:
        log.info(
            f"Dispute désactivée (UMA_DISPUTE_ENABLED=0) | "
            f"{'DISPUTERAIT' if assessment.should_dispute else 'pas de divergence'} "
            f"[{condition_id[:16]}]"
        )
        return DisputeResult.DISABLED

    if not assessment.should_dispute:
        return DisputeResult.NO_DIVERGENCE if not assessment.diverges else DisputeResult.LOW_CONFIDENCE

    # Fenêtre encore ouverte ?
    rem = uma_state.dispute_window_remaining_sec
    if rem is not None and rem <= 0:
        log.warning(f"Fenêtre dispute expirée pour {condition_id[:16]} — trop tard")
        return DisputeResult.WINDOW_EXPIRED

    w3 = _get_web3()
    if w3 is None:
        return DisputeResult.FAILED

    wallet_address, private_key = _get_wallet()
    if wallet_address is None:
        return DisputeResult.FAILED

    try:
        from web3 import Web3
        from eth_account import Account

        # ── Vérification solde USDC ───────────────────────────────────────────
        usdc_contract = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS),
            abi=USDC_ABI,
        )
        usdc_balance = usdc_contract.functions.balanceOf(
            Web3.to_checksum_address(wallet_address)
        ).call() / 1e6

        # Bond + finalFee estimée (10% du bond)
        total_required = assessment.bond_required_usdc * 1.15   # 15% de marge
        if usdc_balance < total_required:
            log.warning(
                f"USDC insuffisant pour dispute | "
                f"disponible={usdc_balance:.2f} requis={total_required:.2f} | "
                f"{condition_id[:16]}"
            )
            return DisputeResult.INSUFFICIENT_USDC

        # ── Approve USDC si nécessaire ────────────────────────────────────────
        if not _ensure_usdc_allowance(
            w3, wallet_address, private_key,
            total_required, gas_price_gwei
        ):
            return DisputeResult.FAILED

        # ── Construction de disputePrice() ────────────────────────────────────
        # On a besoin de l'identifier et du timestamp de la requête UMA originale.
        # On les récupère depuis l'event ProposePrice déjà scanné dans UMAState.
        # L'ancillaryData contient le condition_id Polymarket.
        #
        # Pour Polymarket, l'ancillaryData UMA est le condition_id en bytes.
        cid_clean = condition_id.lower().replace("0x", "")
        cid_padded = cid_clean.ljust(64, "0")[:64]
        ancillary_data = bytes.fromhex(cid_padded)

        # identifier UMA standard pour Polymarket = "YES_OR_NO_QUERY"
        # encodé en bytes32 avec padding
        identifier_str = "YES_OR_NO_QUERY"
        identifier_b32 = identifier_str.encode().ljust(32, b"\x00")[:32]

        # timestamp = market end timestamp (approximé depuis end_date_iso)
        # En pratique, récupéré depuis l'event ProposePrice
        req_timestamp = uma_state.market_closed_at or int(time.time())

        oo = w3.eth.contract(
            address=Web3.to_checksum_address(UMA_OO_ADDRESS),
            abi=UMA_DISPUTE_ABI,
        )

        nonce     = w3.eth.get_transaction_count(Web3.to_checksum_address(wallet_address))
        gas_price = w3.to_wei(gas_price_gwei, "gwei")

        tx = oo.functions.disputePrice(
            Web3.to_checksum_address(POLYMARKET_CTF_ADDRESS),
            identifier_b32,
            req_timestamp,
            ancillary_data,
        ).build_transaction({
            "from":     Web3.to_checksum_address(wallet_address),
            "nonce":    nonce,
            "gasPrice": gas_price,
            "gas":      300_000,
            "chainId":  137,
        })

        signed  = Account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

        log.info(
            f"📤 disputePrice() envoyée | {condition_id[:16]} | "
            f"tx={tx_hash.hex()[:20]}... | "
            f"bond={assessment.bond_required_usdc:.0f} USDC | "
            f"UMA_proposé={'YES' if uma_state.proposed_yes else 'NO'} | "
            f"contrat={'YES' if assessment.contract_says_yes else 'NO'}"
        )

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] != 1:
            log.error(f"❌ disputePrice() échouée | tx={tx_hash.hex()[:20]}")
            return DisputeResult.FAILED

        log.warning(
            f"🚨 DISPUTE DÉCLENCHÉE on-chain | {condition_id[:16]} | "
            f"tx={tx_hash.hex()[:20]}... | "
            f"bond={assessment.bond_required_usdc:.0f} USDC engagé | "
            f"résolution DVM dans 4-6 jours"
        )

        # Log dans onchain_ops
        from monitoring.onchain_logger import log_redeem
        from monitoring.onchain_logger import _append
        _append({
            "op":              "dispute_executed",
            "condition_id":    condition_id,
            "tx_hash":         tx_hash.hex(),
            "bond_usdc":       assessment.bond_required_usdc,
            "proposed_yes":    uma_state.proposed_yes,
            "contract_yes":    assessment.contract_says_yes,
            "observed_temp":   assessment.observed_temp,
            "confidence":      assessment.confidence,
            "gas_used":        receipt["gasUsed"],
        })

        return DisputeResult.DISPUTED

    except Exception as e:
        log.error(f"❌ execute_dispute erreur [{condition_id[:16]}]: {e}")
        return DisputeResult.FAILED


def check_and_dispute_if_needed(
    condition_id: str,
    uma_state,
    spec_cache,
    dry_run: bool = True,
) -> DisputeResult:
    """
    Point d'entrée principal — évalue et dispute si justifié.
    À appeler depuis position_monitor_cycle pendant IN_DISPUTE_WINDOW.
    """
    if uma_state.phase.value != "IN_DISPUTE_WINDOW":
        return DisputeResult.NO_DIVERGENCE

    assessment = assess_proposal(condition_id, uma_state, spec_cache)

    if not assessment.diverges:
        log.debug(
            f"Proposition UMA validée par contrat [{condition_id[:16]}] | "
            f"{'YES' if uma_state.proposed_yes else 'NO'} ✓"
        )
        return DisputeResult.NO_DIVERGENCE

    return execute_dispute(condition_id, uma_state, assessment, dry_run=dry_run)