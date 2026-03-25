"""
Alignement déclaratif entre **observation METAR** (instantané) et **résolution Polymarket**.

Le bot RT ne substitue pas l’oracle on-chain : il expose un *modèle d’interprétation*
pour audit, logs et garde-fous. La valeur légale reste le texte du marché + UMA.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class MetarInterpretation(str, Enum):
    """Comment le signal utilise le dernier METAR."""

    METAR_INSTANT = "metar_instant"
    """Température du dernier METAR = proxy de décision temps réel (défaut)."""

    PROXY_DAILY_HIGH = "proxy_daily_high"
    """
    Heuristique risquée : on traite le METAR comme approximation d’un « high du jour ».
    À n’utiliser que si le contrat l’autorise explicitement ; sinon risque de divergence oracle.
    """

    DOCUMENT_ONLY = "document_only"
    """Signaux à titre informatif ; finance_engine_rt peut filtrer (non implémenté par défaut)."""


@dataclass(frozen=True)
class AlignmentInfo:
    interpretation: MetarInterpretation
    disclaimer: str
    blocks_automated_scalp: bool


_DEFAULT_DISCLAIMER = (
    "METAR = observation instantanée à l’aéroport ; la résolution Polymarket peut reposer sur "
    "une règle différente (ex. max officielle NWS, autre station). Vérifier les règles du marché."
)

_PROXY_DISCLAIMER = (
    "Mode proxy_daily_high : le bot suppose une cohérence forte METAR ↔ max du jour — "
    "souvent **faux** vs oracle. À utiliser avec prudence."
)


def resolve_metar_interpretation(config: dict[str, Any]) -> AlignmentInfo:
    """
    Lit ``metar_interpretation`` ou ``observation_interpretation`` dans la config mapping.

    Envs (override global pour tous les marchés sans champ) :
    ``RT_METAR_INTERPRETATION`` = metar_instant | proxy_daily_high | document_only
    """
    import os

    raw = (
        str(config.get("metar_interpretation") or config.get("observation_interpretation") or "")
        .strip()
        .lower()
    )
    env_raw = (os.getenv("RT_METAR_INTERPRETATION") or "").strip().lower()
    if not raw and env_raw:
        raw = env_raw

    if raw in ("proxy_daily_high", "proxy", "daily_high"):
        return AlignmentInfo(
            interpretation=MetarInterpretation.PROXY_DAILY_HIGH,
            disclaimer=_PROXY_DISCLAIMER,
            blocks_automated_scalp=False,
        )
    if raw in ("document_only", "info", "no_trade"):
        return AlignmentInfo(
            interpretation=MetarInterpretation.DOCUMENT_ONLY,
            disclaimer=_DEFAULT_DISCLAIMER + " Mode document_only : pas d’usage trading automatique recommandé.",
            blocks_automated_scalp=True,
        )
    return AlignmentInfo(
        interpretation=MetarInterpretation.METAR_INSTANT,
        disclaimer=_DEFAULT_DISCLAIMER,
        blocks_automated_scalp=False,
    )


SUPPORTED_RT_WEATHER_VARIABLES = frozenset({"temp_max", "temp_min", "temp_avg"})


def is_supported_weather_variable(name: str) -> bool:
    return (name or "").strip().lower() in SUPPORTED_RT_WEATHER_VARIABLES


# Étiquette pour audit : le signal est un proxy METAR instantané, pas la résolution UMA.
SIGNAL_RESOLUTION_MODEL_INSTANT_METAR = "instant_metar_proxy"
