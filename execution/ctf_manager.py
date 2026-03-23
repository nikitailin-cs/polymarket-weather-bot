"""
execution/ctf_manager.py
------------------------
Gestion on-chain des tokens ERC-1155 Gnosis CTF (Conditional Token Framework).

Sur Polymarket, chaque position est un token ERC-1155 sur Polygon :
  - Token YES d'un marché = token_id (uint256) dans le contrat CTF
  - Token NO  d'un marché = token_id différent dans le même contrat CTF

Ce module implémente les deux opérations critiques que Polymarket API
ne garantit pas en cas d'indisponibilité :

  1. verify_token_balance(token_id, expected_amount)
     → Lit balanceOf on-chain après un ordre fill
     → Détecte les fills silencieux (ordre "matched" mais tokens non reçus)

  2. redeem_position(condition_id, side, wallet_address)
     → Appelle redeemPositions() sur le contrat CTF
     → Récupère les USDC directement on-chain, sans passer par Polymarket API
     → Utilisé quand Gamma API est indisponible ou lent à finaliser

Contrats Polygon mainnet (officiels Polymarket) :
  CTF      : 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
  USDC     : 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
  Exchange : 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E

Références :
  - EIP-1155 : https://eips.ethereum.org/EIPS/eip-1155
  - Gnosis CTF : https://github.com/gnosis/conditional-tokens-contracts
  - Polymarket docs : https://docs.polymarket.com/#contracts
"""
from __future__ import annotations

import os, time, logging
from typing import Optional
from enum import Enum

log = logging.getLogger(__name__)

# Import tardif pour éviter circularités au démarrage
def _log_op(fn, **kwargs):
    try:
        from monitoring.onchain_logger import log_balance_check, log_redeem
        fn(**kwargs)
    except Exception:
        pass  # non-bloquant

# ── Adresses contrats Polygon mainnet ────────────────────────────────────────
CTF_ADDRESS      = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS     = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# bytes32(0) — parentCollectionId pour les marchés simples (pas de split hiérarchique)
ZERO_BYTES32 = b"\x00" * 32

# ABI CTF — fonctions et events utilisés par ce module
CTF_ABI = [
    # ── ERC-1155 ──────────────────────────────────────────────────────────────
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id",      "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "balanceOfBatch",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "accounts", "type": "address[]"},
            {"name": "ids",      "type": "uint256[]"},
        ],
        "outputs": [{"name": "", "type": "uint256[]"}],
    },
    # ── Gnosis CTF ────────────────────────────────────────────────────────────
    {
        "name": "redeemPositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken",     "type": "address"},
            {"name": "parentCollectionId",  "type": "bytes32"},
            {"name": "conditionId",         "type": "bytes32"},
            {"name": "indexSets",           "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "name": "payoutDenominator",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    # ── Events ────────────────────────────────────────────────────────────────
    {
        "name": "ConditionResolution",
        "type": "event",
        "inputs": [
            {"name": "conditionId",      "type": "bytes32",   "indexed": True},
            {"name": "oracle",           "type": "address",   "indexed": True},
            {"name": "questionId",       "type": "bytes32",   "indexed": True},
            {"name": "outcomeSlotCount", "type": "uint256",   "indexed": False},
            {"name": "payoutNumerators", "type": "uint256[]", "indexed": False},
        ],
    },
    {
        "name": "PayoutRedemption",
        "type": "event",
        "inputs": [
            {"name": "redeemer",            "type": "address",   "indexed": True},
            {"name": "collateralToken",     "type": "address",   "indexed": True},
            {"name": "parentCollectionId",  "type": "bytes32",   "indexed": True},
            {"name": "conditionId",         "type": "bytes32",   "indexed": False},
            {"name": "indexSets",           "type": "uint256[]", "indexed": False},
            {"name": "payout",              "type": "uint256",   "indexed": False},
        ],
    },
]

# ABI USDC minimal pour vérifier les balances USDC post-redeem
USDC_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [{"name": "account", "type": "address"}],
        "outputs": [{"name": "",        "type": "uint256"}],
    },
]

# RPC Polygon (même liste que uma_oracle)
POLYGON_RPC_URLS = [
    os.getenv("POLYGON_RPC_URL", ""),
    "https://polygon-rpc.com",
    "https://rpc-mainnet.matic.network",
    "https://matic-mainnet.chainstacklabs.com",
]


class RedeemResult(str, Enum):
    SUCCESS        = "SUCCESS"         # redeemPositions() confirmé on-chain
    NOT_RESOLVED   = "NOT_RESOLVED"    # payoutDenominator == 0 → pas encore résolu
    ALREADY_REDEEMED = "ALREADY_REDEEMED"  # balance déjà 0 avant tentative
    FAILED         = "FAILED"          # tx échouée ou RPC indisponible
    DRY_RUN        = "DRY_RUN"         # mode simulation


def _get_web3():
    """Connexion Web3 Polygon avec fallback multi-RPC."""
    try:
        from web3 import Web3
    except ImportError:
        log.error("web3 non installé. pip install web3")
        return None
    for url in POLYGON_RPC_URLS:
        if not url:
            continue
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                log.debug(f"CTF Manager: connecté via {url[:40]}")
                return w3
        except Exception:
            continue
    log.warning("CTF Manager: aucun RPC Polygon disponible")
    return None


def _parse_condition_id(condition_id: str) -> bytes:
    """
    Convertit un condition_id Polymarket en bytes32.
    Les condition_id Polymarket sont des hex strings (avec ou sans 0x).
    Certains sont courts (< 64 hex chars) → padding à droite avec 0.
    """
    cid = condition_id.lower().replace("0x", "")
    # Pad à 64 caractères hex = 32 bytes
    cid = cid.ljust(64, "0")[:64]
    return bytes.fromhex(cid)


def _get_wallet_address() -> Optional[str]:
    """
    Dérive l'adresse wallet depuis la clé privée dans settings.
    Utilise py_clob_client si disponible, sinon eth_account directement.
    """
    private_key = os.getenv("PRIVATE_KEY", "")
    if not private_key:
        log.error("CTF Manager: PRIVATE_KEY non configurée")
        return None
    try:
        from eth_account import Account
        acct = Account.from_key(private_key)
        return acct.address
    except Exception as e:
        log.error(f"CTF Manager: impossible de dériver l'adresse wallet: {e}")
        return None


# ── API publique ──────────────────────────────────────────────────────────────

def verify_token_balance(
    token_id:        str,
    expected_tokens: float,
    wallet_address:  Optional[str] = None,
    tolerance_pct:   float = 0.05,   # tolérance 5% pour les arrondis
) -> tuple[bool, float]:
    """
    Vérifie que le wallet détient bien les tokens ERC-1155 attendus après un fill.

    Sur Polymarket, les tokens sont en unité USDC × 10^6 (6 décimales).
    expected_tokens = size_usd / entry_price (nombre de shares achetés).

    Args:
        token_id        : identifiant du token (uint256 string)
        expected_tokens : nombre de tokens attendus (en unités USDC, pas wei)
        wallet_address  : adresse à vérifier (auto-dérivée si None)
        tolerance_pct   : tolérance sur la différence (défaut 5%)

    Returns:
        (ok: bool, actual_balance: float)
        ok = True si balance >= expected × (1 - tolerance_pct)
    """
    w3 = _get_web3()
    if w3 is None:
        return False, 0.0

    address = wallet_address or _get_wallet_address()
    if address is None:
        return False, 0.0

    try:
        from web3 import Web3
        ctf = w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_ABI,
        )
        token_id_int = int(token_id)
        balance_raw  = ctf.functions.balanceOf(
            Web3.to_checksum_address(address),
            token_id_int,
        ).call()

        # Les tokens CTF ont 6 décimales (USDC standard)
        actual_balance = balance_raw / 1e6

        min_expected = expected_tokens * (1.0 - tolerance_pct)
        ok = actual_balance >= min_expected

        if not ok:
            log.warning(
                f"⚠️ Balance token insuffisante | token={token_id[:20]}... | "
                f"attendu={expected_tokens:.4f} | reçu={actual_balance:.4f} | "
                f"wallet={address[:12]}..."
            )
        else:
            log.debug(
                f"✅ Balance token OK | reçu={actual_balance:.4f} | "
                f"attendu≥{min_expected:.4f}"
            )

        # Persistance on-chain log
        _log_op(
            lambda **k: __import__('monitoring.onchain_logger', fromlist=['log_balance_check']).log_balance_check(**k),
            condition_id    = "",          # pas disponible ici — enrichi par order_manager
            token_id        = token_id,
            expected_tokens = expected_tokens,
            actual_balance  = actual_balance,
            ok              = ok,
            wallet_address  = address,
        )

        return ok, actual_balance

    except Exception as e:
        log.error(f"CTF balanceOf erreur [{token_id[:20]}...]: {e}")
        return False, 0.0


def is_condition_resolved(condition_id: str) -> bool:
    """
    Vérifie on-chain si un marché est résolu via payoutDenominator.
    payoutDenominator > 0 ↔ le marché a été résolu par l'oracle.
    C'est la source de vérité décentralisée — indépendante de Gamma.
    """
    w3 = _get_web3()
    if w3 is None:
        return False
    try:
        from web3 import Web3
        ctf = w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_ABI,
        )
        cid_bytes = _parse_condition_id(condition_id)
        denom = ctf.functions.payoutDenominator(cid_bytes).call()
        resolved = denom > 0
        log.debug(
            f"CTF payoutDenominator [{condition_id[:16]}]: "
            f"{denom} → {'résolu' if resolved else 'non résolu'}"
        )
        return resolved
    except Exception as e:
        log.debug(f"CTF payoutDenominator erreur [{condition_id[:16]}]: {e}")
        return False


def redeem_position(
    condition_id:   str,
    side:           str,          # "YES" ou "NO"
    dry_run:        bool = True,
    wallet_address: Optional[str] = None,
    gas_price_gwei: float = 100.0,  # Polygon : 100-200 gwei standard
) -> RedeemResult:
    """
    Appelle redeemPositions() sur le contrat CTF Gnosis pour récupérer
    les USDC d'une position gagnante, directement on-chain.

    Utilisé quand :
      - Gamma API est lente ou indisponible
      - Position résolue mais non liquidée après MAX_ORACLE_WAIT_HOURS
      - Vérification autonome sans dépendance à Polymarket Labs

    Mécanique contractuelle :
      - indexSets = [1] pour YES (bit 0), [2] pour NO (bit 1)
      - parentCollectionId = bytes32(0) pour les marchés simples
      - Le contrat vérifie que payoutDenominator > 0 (marché résolu)
      - Brûle les tokens gagnants et transfère les USDC au wallet

    Args:
        condition_id   : condition_id Polymarket (0x...)
        side           : "YES" ou "NO"
        dry_run        : True = simulation, False = tx réelle
        wallet_address : adresse à créditer (auto-dérivée si None)
        gas_price_gwei : prix du gas en gwei (Polygon)

    Returns:
        RedeemResult enum
    """
    if dry_run:
        log.info(
            f"[DRY-RUN] redeem_position simulé | {condition_id[:16]} | side={side}"
        )
        return RedeemResult.DRY_RUN

    # ── Vérifications préalables ───────────────────────────────────────────────
    if not is_condition_resolved(condition_id):
        log.warning(
            f"redeem_position: marché {condition_id[:16]} pas encore résolu on-chain "
            f"(payoutDenominator == 0) — abandon"
        )
        return RedeemResult.NOT_RESOLVED

    w3 = _get_web3()
    if w3 is None:
        return RedeemResult.FAILED

    address = wallet_address or _get_wallet_address()
    if address is None:
        return RedeemResult.FAILED

    private_key = os.getenv("PRIVATE_KEY", "")
    if not private_key:
        log.error("CTF redeem: PRIVATE_KEY manquante")
        return RedeemResult.FAILED

    try:
        from web3 import Web3
        from eth_account import Account

        ctf = w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_ABI,
        )

        cid_bytes  = _parse_condition_id(condition_id)

        # indexSets : YES = bit 0 = [1], NO = bit 1 = [2]
        # On tente les deux pour récupérer ce qui reste (positions partielles)
        index_sets = [1] if side.upper() == "YES" else [2]

        # Vérifie la balance USDC avant pour calculer le gain réel
        usdc = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS),
            abi=USDC_ABI,
        )
        usdc_before = usdc.functions.balanceOf(
            Web3.to_checksum_address(address)
        ).call() / 1e6

        # ── Construction et envoi de la transaction ───────────────────────────
        nonce     = w3.eth.get_transaction_count(Web3.to_checksum_address(address))
        gas_price = w3.to_wei(gas_price_gwei, "gwei")

        tx = ctf.functions.redeemPositions(
            Web3.to_checksum_address(USDC_ADDRESS),   # collateralToken
            ZERO_BYTES32,                              # parentCollectionId
            cid_bytes,                                 # conditionId
            index_sets,                                # indexSets
        ).build_transaction({
            "from":     Web3.to_checksum_address(address),
            "nonce":    nonce,
            "gasPrice": gas_price,
            "gas":      200_000,   # estimation conservative pour redeemPositions
            "chainId":  137,       # Polygon mainnet
        })

        signed_tx  = Account.sign_transaction(tx, private_key)
        tx_hash    = w3.eth.send_raw_transaction(signed_tx.raw_transaction)

        log.info(
            f"📤 redeemPositions tx envoyée | {condition_id[:16]} | "
            f"side={side} | tx={tx_hash.hex()[:20]}..."
        )

        # ── Attente de confirmation (max 60s) ─────────────────────────────────
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] != 1:
            log.error(
                f"❌ redeemPositions échouée | tx={tx_hash.hex()[:20]} | "
                f"status={receipt['status']}"
            )
            return RedeemResult.FAILED

        # ── Calcul du gain USDC effectif ──────────────────────────────────────
        usdc_after  = usdc.functions.balanceOf(
            Web3.to_checksum_address(address)
        ).call() / 1e6
        usdc_gained = usdc_after - usdc_before

        log.info(
            f"✅ redeemPositions confirmée | {condition_id[:16]} | "
            f"side={side} | +{usdc_gained:.4f} USDC | "
            f"tx={tx_hash.hex()[:20]}... | "
            f"gas={receipt['gasUsed']}"
        )
        _log_op(
            lambda **k: __import__('monitoring.onchain_logger', fromlist=['log_redeem']).log_redeem(**k),
            condition_id = condition_id,
            side         = side,
            result       = RedeemResult.SUCCESS.value,
            tx_hash      = tx_hash.hex(),
            usdc_gained  = usdc_gained,
            gas_used     = receipt['gasUsed'],
            dry_run      = False,
        )
        return RedeemResult.SUCCESS

    except Exception as e:
        log.error(f"❌ redeem_position erreur [{condition_id[:16]}]: {e}")
        return RedeemResult.FAILED


def get_usdc_balance(wallet_address: Optional[str] = None) -> float:
    """
    Retourne la balance USDC on-chain du wallet (en USDC, pas en wei).
    Utile pour cross-check avec la bankroll du bot.
    """
    w3 = _get_web3()
    if w3 is None:
        return 0.0
    address = wallet_address or _get_wallet_address()
    if address is None:
        return 0.0
    try:
        from web3 import Web3
        usdc = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS),
            abi=USDC_ABI,
        )
        raw = usdc.functions.balanceOf(
            Web3.to_checksum_address(address)
        ).call()
        return raw / 1e6
    except Exception as e:
        log.debug(f"get_usdc_balance erreur: {e}")
        return 0.0


if __name__ == "__main__":
    import sys
    print("🔗 CTF Manager — Polygon mainnet")
    print(f"   CTF      : {CTF_ADDRESS}")
    print(f"   USDC     : {USDC_ADDRESS}")
    print(f"   Exchange : {EXCHANGE_ADDRESS}")

    wallet = _get_wallet_address()
    print(f"   Wallet   : {wallet or 'PRIVATE_KEY non configurée'}")

    if wallet:
        usdc_bal = get_usdc_balance(wallet)
        print(f"   USDC on-chain : {usdc_bal:.4f} USDC")

    if len(sys.argv) > 1:
        cid = sys.argv[1]
        resolved = is_condition_resolved(cid)
        print(f"\n   Marché {cid[:20]}... → résolu={resolved}")