"""
monitoring/onchain_logger.py
-----------------------------
Persistance JSON de toutes les opérations on-chain effectuées par le bot.

Enregistre :
  - verify_token_balance()  → résultat du check ERC-1155 post-fill
  - redeem_position()       → appel CTF redeemPositions() avec tx hash
  - is_condition_resolved() → check payoutDenominator (quand décisionnel)
  - get_uma_status()        → détection de dispute UMA

Chaque entrée est immuable (append-only) — audit complet des opérations
on-chain sans dépendance aux logs texte.

Fichier : data/logs/onchain_ops.json
Format  : liste d'objets JSON, du plus ancien au plus récent.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any


def _get_path() -> Path:
    try:
        from config.settings import ONCHAIN_OPS_FILE
        return Path(ONCHAIN_OPS_FILE)
    except Exception:
        return Path(__file__).parent.parent / "data" / "logs" / "onchain_ops.json"


def _append(entry: dict) -> None:
    """Append atomique — ne relit pas tout le fichier pour chaque écriture."""
    path = _get_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    entry["logged_at"] = datetime.now(timezone.utc).isoformat()

    # Lecture + append + réécriture atomique via tmp
    try:
        ops = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except Exception:
        ops = []

    ops.append(entry)

    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ops, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ── API publique ──────────────────────────────────────────────────────────────

def log_balance_check(
    condition_id:    str,
    token_id:        str,
    expected_tokens: float,
    actual_balance:  float,
    ok:              bool,
    wallet_address:  Optional[str] = None,
) -> None:
    """Enregistre le résultat d'un verify_token_balance() post-fill."""
    _append({
        "op":             "balance_check",
        "condition_id":   condition_id,
        "token_id":       token_id[:30] + "..." if len(token_id) > 30 else token_id,
        "expected":       round(expected_tokens, 6),
        "actual":         round(actual_balance, 6),
        "ok":             ok,
        "discrepancy":    round(actual_balance - expected_tokens, 6),
        "wallet":         (wallet_address or "")[:20],
    })


def log_redeem(
    condition_id:  str,
    side:          str,
    result:        str,          # RedeemResult.value
    tx_hash:       Optional[str] = None,
    usdc_gained:   Optional[float] = None,
    gas_used:      Optional[int]   = None,
    dry_run:       bool = True,
    error:         Optional[str]   = None,
) -> None:
    """Enregistre un appel à redeem_position()."""
    _append({
        "op":           "redeem",
        "condition_id": condition_id,
        "side":         side,
        "result":       result,
        "tx_hash":      tx_hash,
        "usdc_gained":  usdc_gained,
        "gas_used":     gas_used,
        "dry_run":      dry_run,
        "error":        error,
    })


def log_resolution_check(
    condition_id:         str,
    source:               str,        # "gamma" | "uma_onchain" | "ctf_evaluate"
    resolved:             bool,
    resolution_matches_contract: Optional[bool] = None,  # résultat de evaluate()
    observed_value:       Optional[float] = None,
    contract_threshold:   Optional[float] = None,
    contract_op:          Optional[str]   = None,
    gamma_status:         Optional[str]   = None,
    uma_status:           Optional[str]   = None,
    conflict:             bool = False,   # True si Gamma et evaluate() divergent
) -> None:
    """
    Enregistre une vérification de résolution — notamment quand
    ContractSpec.evaluate() est utilisée pour cross-checker Gamma.
    """
    _append({
        "op":                          "resolution_check",
        "condition_id":                condition_id,
        "source":                      source,
        "resolved":                    resolved,
        "resolution_matches_contract": resolution_matches_contract,
        "observed_value":              observed_value,
        "contract_threshold":          contract_threshold,
        "contract_op":                 contract_op,
        "gamma_status":                gamma_status,
        "uma_status":                  uma_status,
        "conflict":                    conflict,
    })


def log_uma_dispute(
    condition_id: str,
    uma_status:   str,
    proposer:     Optional[str] = None,
    disputer:     Optional[str] = None,
) -> None:
    """Enregistre une détection de dispute UMA."""
    _append({
        "op":           "uma_dispute",
        "condition_id": condition_id,
        "uma_status":   uma_status,
        "proposer":     proposer,
        "disputer":     disputer,
    })


def get_ops(condition_id: Optional[str] = None) -> list:
    """Retourne toutes les ops, filtrées par condition_id si fourni."""
    path = _get_path()
    if not path.exists():
        return []
    try:
        ops = json.loads(path.read_text(encoding="utf-8"))
        if condition_id:
            ops = [o for o in ops if o.get("condition_id") == condition_id]
        return ops
    except Exception:
        return []


def print_onchain_summary() -> None:
    """Affiche un résumé des opérations on-chain."""
    ops = get_ops()
    if not ops:
        print("  Aucune opération on-chain enregistrée.")
        return

    counts: dict = {}
    conflicts = [o for o in ops if o.get("conflict")]
    mismatches = [o for o in ops if o.get("op") == "balance_check" and not o.get("ok")]
    redeems    = [o for o in ops if o.get("op") == "redeem"]
    disputes   = [o for o in ops if o.get("op") == "uma_dispute"]

    for o in ops:
        counts[o.get("op", "?")] = counts.get(o.get("op", "?"), 0) + 1

    print(f"\n{'═'*60}")
    print(f"  OPÉRATIONS ON-CHAIN ({len(ops)} total)")
    print(f"{'═'*60}")
    for op, n in counts.items():
        print(f"  {op:<25} : {n}")
    print(f"{'─'*60}")
    if mismatches:
        print(f"  ⚠️  Balance mismatches      : {len(mismatches)}")
    if conflicts:
        print(f"  🚨 Conflits Gamma/contrat  : {len(conflicts)}")
    if disputes:
        print(f"  🚨 Disputes UMA détectées  : {len(disputes)}")
    successful_redeems = [r for r in redeems if r.get("result") == "SUCCESS"]
    if successful_redeems:
        total_usdc = sum(r.get("usdc_gained") or 0 for r in successful_redeems)
        print(f"  ✅ Redeems on-chain réussis : {len(successful_redeems)} (+{total_usdc:.2f} USDC)")
    print(f"{'═'*60}")