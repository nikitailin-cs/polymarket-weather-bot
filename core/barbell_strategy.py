"""
Stratégie « haltère » (Barbell) pour marchés de probabilité.

- **Safe** : prix du token acheté dans une bande haute (ex. 0,90–0,99 $) avec
  confiance modèle élevée sur l’issue — rendement modeste, volatilité faible.
- **Asymétrique** : token bon marché (ex. 0,002–0,50 $) lorsque le modèle estime un
  edge net suffisant — risque plus élevé, payoff optionnel.

Les paramètres passent par des variables d’environnement (préfixe ``BARBELL_``).
Activation : ``BARBELL_ENABLED=1``.

Utilisé par : ``signals/edge_detector`` (scanner) et ``bots/bot1/execution/finance_engine``
(NOAA) — même classification et mêmes budgets jambe lorsque le haltère est actif.
Référence bankroll côté NOAA : ``BARBELL_BANKROLL_USDC`` (défaut : ``MAX_PORTFOLIO_USD``).
Si ``BARBELL_ENABLED=0`` sur NOAA, seul ``FINANCE_NOAA_MIN_EV`` (défaut 0) filtre les trades
(les anciennes conditions A/B locales ont été retirées).
"""
from __future__ import annotations

import os
from typing import Literal, Optional

BarbellLeg = Literal["safe", "asymmetric"]


def barbell_enabled() -> bool:
    return (os.getenv("BARBELL_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")


def _f(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Prix du token acheté (approx. probabilité implicite côté marché pour ce token).
def token_entry_price(side: str, market_yes_prob: float) -> float:
    p = float(market_yes_prob)
    p = max(0.0, min(1.0, p))
    if (side or "").upper() == "YES":
        return p
    return 1.0 - p


def our_p_win_for_side(side: str, our_yes_prob: float) -> float:
    if (side or "").upper() == "YES":
        return float(our_yes_prob)
    return max(0.0, min(1.0, 1.0 - float(our_yes_prob)))


def classify_barbell_leg(
    side: str,
    our_yes_prob: float,
    market_yes_prob: float,
    net_edge: float,
) -> Optional[BarbellLeg]:
    """
    Retourne ``safe``, ``asymmetric``, ou ``None`` si le couple (prix, edge, modèle)
    ne rentre dans aucune jambe haltère.
    """
    entry = token_entry_price(side, market_yes_prob)
    p_win = our_p_win_for_side(side, our_yes_prob)

    safe_lo = _f("BARBELL_SAFE_PRICE_MIN", 0.90)
    safe_hi = _f("BARBELL_SAFE_PRICE_MAX", 0.99)
    safe_model_min = _f("BARBELL_SAFE_MIN_MODEL_P_WIN", 0.92)
    safe_edge_min = _f("BARBELL_SAFE_MIN_NET_EDGE", 0.005)

    asym_lo = _f("BARBELL_ASYMMETRIC_PRICE_MIN", 0.002)
    asym_hi = _f("BARBELL_ASYMMETRIC_PRICE_MAX", 0.50)
    asym_edge_min = _f("BARBELL_ASYMMETRIC_MIN_NET_EDGE", 0.05)

    # Safe : quasi-certitude modèle + prix élevé du token (on « paie » le carry).
    if safe_lo <= entry <= safe_hi and p_win >= safe_model_min and net_edge >= safe_edge_min:
        return "safe"

    # Asymétrique : ticket peu cher + edge net (données vs marché).
    if asym_lo <= entry <= asym_hi and net_edge >= asym_edge_min:
        return "asymmetric"

    return None


def barbell_bucket_budgets_usd(bankroll: float) -> dict[BarbellLeg, float]:
    """Budget notionnel max par jambe (fractions de la bankroll)."""
    safe_frac = _f("BARBELL_SAFE_BANKROLL_FRACTION", 0.85)
    asym_frac = _f("BARBELL_ASYMMETRIC_BANKROLL_FRACTION", 0.15)
    s = max(0.0, float(bankroll)) * max(0.0, min(1.0, safe_frac))
    a = max(0.0, float(bankroll)) * max(0.0, min(1.0, asym_frac))
    return {"safe": s, "asymmetric": a}


def barbell_stake_multiplier(leg: BarbellLeg) -> float:
    """Ajuste Kelly par jambe (réduit le safe, modère l’asymétrique)."""
    if leg == "safe":
        return _f("BARBELL_SAFE_KELLY_MULTIPLIER", 0.35)
    return _f("BARBELL_ASYMMETRIC_KELLY_MULTIPLIER", 0.50)


def cap_stake_to_bucket(
    stake_usd: float,
    leg: BarbellLeg,
    bankroll: float,
    already_deployed_safe: float,
    already_deployed_asym: float,
) -> float:
    """Plafonne la mise pour ne pas dépasser le budget jambe restant."""
    budgets = barbell_bucket_budgets_usd(bankroll)
    if leg == "safe":
        cap = max(0.0, budgets["safe"] - max(0.0, already_deployed_safe))
    else:
        cap = max(0.0, budgets["asymmetric"] - max(0.0, already_deployed_asym))
    return max(0.0, min(stake_usd, cap))
