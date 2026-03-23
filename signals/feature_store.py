"""
signals/feature_store.py
-------------------------
Persistance structurée de chaque vecteur de features produit par le bot.

Objectif : constituer un dataset d'entraînement complet pour tout modèle ML
futur, sans modifier le pipeline de trading. Chaque prédiction est enregistrée
avec ses features d'entrée ET son outcome réel (rempli a posteriori par
update_outcomes()).

Schéma du vecteur de features (FeatureVector) :
  ── Identifiants ────────────────────────────────────────────────
  condition_id      str    identifiant du marché Polymarket
  question          str    texte brut de la question
  city              str    ville cible
  date_str          str    date de l'événement (YYYY-MM-DD)
  recorded_at       str    ISO timestamp de la prédiction

  ── Cible contractuelle ─────────────────────────────────────────
  weather_variable  str    temp_max / temp_min / precip / ...
  threshold_value   float  seuil (ex: 15.0)
  threshold_op      str    gte / lte / eq / gt / lt
  threshold_unit    str    celsius / fahrenheit / mm
  modifier          str    above / below / exact

  ── Horizon temporel ────────────────────────────────────────────
  days_to_expiry    int    jours jusqu'à l'échéance
  month             int    mois de l'événement (1-12)

  ── Prévisions météo multi-sources ──────────────────────────────
  forecast_open_meteo   float   prévision Open-Meteo (°C)
  forecast_owm          float   prévision OWM (°C)
  forecast_noaa         float   prévision NOAA (°C, US only)
  forecast_weatherapi   float   prévision WeatherAPI (°C)
  forecast_tomorrow_io  float   prévision Tomorrow.io (°C)
  forecast_consensus    float   moyenne pondérée
  forecast_confidence   float   confiance agrégée (0-1)
  forecast_n_sources    int     nombre de sources disponibles
  forecast_std          float   écart-type entre sources

  ── Correction de biais ─────────────────────────────────────────
  bias_correction   float   correction biais ville (°C)
  corrected_forecast float  forecast_consensus + bias_correction

  ── Incertitude ─────────────────────────────────────────────────
  sigma             float   sigma gaussien utilisé pour P(yes)

  ── Probabilités par couche ─────────────────────────────────────
  p_forecast        float   P(yes) depuis prévision météo
  p_climate         float   P(yes) depuis climatologie historique
  p_trend           float   P(yes) depuis tendance récente
  p_model           float   P(yes) depuis modèle ML (si actif)
  p_final           float   P(yes) fusionnée finale
  layers_used       int     nombre de couches activées

  ── Contexte marché ─────────────────────────────────────────────
  market_yes_prob   float   prix YES du marché (probabilité implicite)
  edge_brut         float   p_final - market_yes_prob
  side              str     YES / NO
  kelly_stake_usd   float   mise Kelly calculée

  ── Oracle ──────────────────────────────────────────────────────
  oracle_alignment  float   score alignement oracle (0-1)
  resolution_source str     uma / pyth / manual

  ── Outcome réel (rempli a posteriori) ──────────────────────────
  observed_value    float   valeur météo réellement observée
  outcome_yes       bool    True si YES s'est réalisé
  pnl_usd           float   PnL réalisé sur ce trade
  filled            bool    True si l'ordre a été exécuté

Stockage : JSON Lines (.jsonl) — une prédiction par ligne, append-only.
Format choisi pour sa lisibilité, sa robustesse aux crashes, et sa
compatibilité directe avec pandas.read_json(lines=True).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

# ── Chemin de stockage ────────────────────────────────────────────────────────
def _get_store_path() -> Path:
    try:
        from config.settings import _ROOT
        path = Path(_ROOT) / "data" / "ml" / "feature_store.jsonl"
    except Exception:
        path = Path(__file__).parent.parent / "data" / "ml" / "feature_store.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ── Dataclass vecteur de features ────────────────────────────────────────────

@dataclass
class FeatureVector:
    """
    Vecteur de features complet pour une prédiction.
    Chaque champ manquant vaut None — le modèle doit gérer les NaN.
    """
    # Identifiants
    condition_id:       str   = ""
    question:           str   = ""
    city:               str   = ""
    date_str:           str   = ""
    recorded_at:        str   = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Cible contractuelle
    weather_variable:   str            = "temp_max"
    threshold_value:    float          = 0.0
    threshold_op:       str            = "gte"
    threshold_unit:     str            = "celsius"
    modifier:           str            = "above"

    # Horizon
    days_to_expiry:     int            = 0
    month:              int            = 1

    # Prévisions multi-sources
    forecast_open_meteo:   Optional[float] = None
    forecast_owm:          Optional[float] = None
    forecast_noaa:         Optional[float] = None
    forecast_weatherapi:   Optional[float] = None
    forecast_tomorrow_io:  Optional[float] = None
    forecast_consensus:    Optional[float] = None
    forecast_confidence:   float           = 0.0
    forecast_n_sources:    int             = 0
    forecast_std:          Optional[float] = None

    # Biais et correction
    bias_correction:       float           = 0.0
    corrected_forecast:    Optional[float] = None

    # Incertitude
    sigma:                 float           = 2.0

    # Probabilités par couche
    p_forecast:            Optional[float] = None
    p_climate:             Optional[float] = None
    p_trend:               Optional[float] = None
    p_model:               Optional[float] = None   # modèle ML externe
    p_final:               float           = 0.5
    layers_used:           int             = 1

    # Contexte marché
    market_yes_prob:       float           = 0.5
    edge_brut:             float           = 0.0
    side:                  str             = "NO"
    kelly_stake_usd:       float           = 0.0

    # Oracle
    oracle_alignment:      float           = 0.0
    resolution_source:     str             = "uma"

    # Outcome réel (rempli a posteriori)
    observed_value:        Optional[float] = None
    outcome_yes:           Optional[bool]  = None
    pnl_usd:               Optional[float] = None
    filled:                bool            = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FeatureVector":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

    def compute_derived(self) -> None:
        """Calcule les features dérivées depuis les sources brutes."""
        # Nombre de sources disponibles
        sources = [
            self.forecast_open_meteo,
            self.forecast_owm,
            self.forecast_noaa,
            self.forecast_weatherapi,
            self.forecast_tomorrow_io,
        ]
        vals = [v for v in sources if v is not None]
        self.forecast_n_sources = len(vals)

        # Écart-type entre sources
        if len(vals) > 1:
            mean = sum(vals) / len(vals)
            variance = sum((v - mean) ** 2 for v in vals) / len(vals)
            self.forecast_std = round(math.sqrt(variance), 3)

        # Edge brut
        self.edge_brut = round(self.p_final - self.market_yes_prob, 4)


# ── API publique ──────────────────────────────────────────────────────────────

def record_features(fv: FeatureVector) -> None:
    """
    Persiste un vecteur de features en mode append-only (JSON Lines).
    Thread-safe par append atomique (os.open avec O_APPEND).
    """
    fv.compute_derived()
    path = _get_store_path()
    line = json.dumps(fv.to_dict(), ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def update_outcomes(
    condition_id: str,
    outcome_yes:  bool,
    observed_value: Optional[float] = None,
    pnl_usd:      Optional[float]   = None,
) -> int:
    """
    Rempli l'outcome réel a posteriori pour toutes les prédictions
    correspondant à un condition_id.

    Retourne le nombre de lignes mises à jour.
    Utilise une réécriture complète du fichier — appel rare (à la fermeture d'une position).
    """
    path = _get_store_path()
    if not path.exists():
        return 0

    lines      = path.read_text(encoding="utf-8").splitlines()
    updated    = 0
    new_lines  = []

    for line in lines:
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            if d.get("condition_id") == condition_id and d.get("outcome_yes") is None:
                d["outcome_yes"]    = outcome_yes
                d["observed_value"] = observed_value
                d["pnl_usd"]        = pnl_usd
                updated += 1
            new_lines.append(json.dumps(d, ensure_ascii=False))
        except Exception:
            new_lines.append(line)

    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return updated


def load_dataframe(labeled_only: bool = False):
    """
    Charge le feature store en pandas DataFrame.

    Args:
        labeled_only : si True, retourne seulement les lignes avec outcome_yes rempli

    Returns:
        pd.DataFrame ou None si pandas non disponible
    """
    try:
        import pandas as pd
    except ImportError:
        return None

    path = _get_store_path()
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()

    df = pd.read_json(path, lines=True)
    if labeled_only:
        df = df[df["outcome_yes"].notna()]
    return df


def get_stats() -> Dict[str, Any]:
    """Retourne des statistiques sur le feature store."""
    path = _get_store_path()
    if not path.exists():
        return {"total": 0, "labeled": 0, "cities": [], "size_mb": 0}

    lines   = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    labeled = 0
    cities: set = set()

    for line in lines:
        try:
            d = json.loads(line)
            if d.get("outcome_yes") is not None:
                labeled += 1
            if d.get("city"):
                cities.add(d["city"])
        except Exception:
            pass

    return {
        "total":    len(lines),
        "labeled":  labeled,
        "unlabeled": len(lines) - labeled,
        "cities":   sorted(cities),
        "size_mb":  round(path.stat().st_size / 1e6, 3),
        "path":     str(path),
    }


def print_store_summary() -> None:
    stats = get_stats()
    print(f"\n{'═'*60}")
    print(f"  FEATURE STORE ML")
    print(f"{'═'*60}")
    print(f"  Total prédictions : {stats['total']}")
    print(f"  Avec outcome      : {stats['labeled']} "
          f"({stats['labeled']/max(stats['total'],1)*100:.0f}%)")
    print(f"  Sans outcome      : {stats['unlabeled']}")
    print(f"  Villes couvertes  : {len(stats['cities'])}")
    print(f"  Taille            : {stats['size_mb']} MB")
    print(f"  Fichier           : {stats['path']}")
    print(f"{'═'*60}")


def build_feature_vector_from_detail(
    condition_id:   str,
    question:       str,
    city:           str,
    date_str:       str,
    detail:         dict,          # depuis compute_yes_prob
    market_yes_prob: float,
    side:           str,
    kelly_stake_usd: float,
    oracle_alignment: float        = 0.0,
    resolution_source: str         = "uma",
    sources_detail: Optional[dict] = None,  # dict source → valeur
    spec = None,                   # ContractSpec optionnel
) -> FeatureVector:
    """
    Construit un FeatureVector depuis les données disponibles dans le pipeline.
    Utilisé par probability_engine après compute_yes_prob.
    """
    fv = FeatureVector(
        condition_id      = condition_id,
        question          = question,
        city              = city,
        date_str          = date_str,
        weather_variable  = "temp_max",
        days_to_expiry    = detail.get("days_to_expiry", 0),
        month             = _month_from_date(date_str),
        forecast_consensus= detail.get("forecast_c"),
        forecast_confidence= detail.get("sigma", 2.0),  # approximation
        bias_correction   = detail.get("bias_corr", 0.0),
        corrected_forecast= detail.get("forecast_c"),
        sigma             = detail.get("sigma", 2.0),
        p_forecast        = detail.get("p_forecast"),
        p_climate         = detail.get("p_climate"),
        p_trend           = detail.get("p_trend"),
        p_model           = detail.get("p_model"),
        p_final           = detail.get("p_final", 0.5),
        layers_used       = detail.get("layers_used", 1),
        market_yes_prob   = market_yes_prob,
        side              = side,
        kelly_stake_usd   = kelly_stake_usd,
        oracle_alignment  = oracle_alignment,
        resolution_source = resolution_source,
        filled            = False,
    )

    # Extrait les prévisions par source
    if sources_detail:
        fv.forecast_open_meteo  = sources_detail.get("open_meteo")
        fv.forecast_owm         = sources_detail.get("owm")
        fv.forecast_noaa        = sources_detail.get("noaa")
        fv.forecast_weatherapi  = sources_detail.get("weatherapi")
        fv.forecast_tomorrow_io = sources_detail.get("tomorrow_io")

    # Extrait les infos contractuelles depuis le spec
    if spec is not None:
        fv.weather_variable = spec.weather_variable
        fv.threshold_value  = spec.threshold_value
        fv.threshold_op     = spec.threshold_op
        fv.threshold_unit   = spec.threshold_unit

    return fv


def _month_from_date(date_str: str) -> int:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").month
    except Exception:
        return 1


if __name__ == "__main__":
    print_store_summary()