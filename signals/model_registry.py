"""
signals/model_registry.py
--------------------------
Singleton qui charge, cache et expose le modèle de prédiction actif.

Configuration via variables d'environnement (dans .env) :
  MODEL_PATH     : chemin vers le fichier modèle (.pkl ou .onnx)
                   Si absent → StatisticalModel (pipeline actuel)
  MODEL_WEIGHT   : poids du modèle ML dans l'ensemble (0.0-1.0, défaut 0.6)
                   Si MODEL_PATH fourni, crée automatiquement un EnsembleModel
                   combinant StatisticalModel (1-weight) + modèle ML (weight)
  MODEL_ENDPOINT : URL d'un service de prédiction HTTP (RemoteModel)
  MODEL_API_KEY  : clé API pour MODEL_ENDPOINT

Comportement :
  - Sans MODEL_PATH ni MODEL_ENDPOINT → StatisticalModel (défaut stable)
  - Avec MODEL_PATH (.pkl) → EnsembleModel(Statistical + Sklearn)
  - Avec MODEL_PATH (.onnx) → EnsembleModel(Statistical + Onnx)
  - Avec MODEL_ENDPOINT → EnsembleModel(Statistical + Remote)
  - MODEL_WEIGHT=1.0 → modèle ML seul (no ensemble)
  - MODEL_WEIGHT=0.0 → StatisticalModel seul (ignore MODEL_PATH)

Hot-reload : reload_model() recharge le modèle sans redémarrer le bot.
"""
from __future__ import annotations

import os
import logging
import threading
from pathlib import Path
from typing import Optional

from signals.model_interface import (
    WeatherModel, ModelPrediction,
    StatisticalModel, SklearnModel, OnnxModel,
    EnsembleModel, RemoteModel,
)

log = logging.getLogger(__name__)

# ── Singleton thread-safe ─────────────────────────────────────────────────────
_lock:  threading.RLock    = threading.RLock()
_model: Optional[WeatherModel] = None


def _build_model() -> WeatherModel:
    """
    Construit le modèle selon la configuration .env.
    Appelé une seule fois au démarrage (puis sur reload_model()).
    """
    model_path     = os.getenv("MODEL_PATH",     "").strip()
    model_endpoint = os.getenv("MODEL_ENDPOINT", "").strip()
    model_api_key  = os.getenv("MODEL_API_KEY",  "").strip()
    model_weight   = float(os.getenv("MODEL_WEIGHT", "0.6"))

    stat_model = StatisticalModel()

    # ── Cas 1 : aucune configuration → modèle statistique seul ───────────────
    if not model_path and not model_endpoint:
        log.info("ModelRegistry: StatisticalModel (aucun MODEL_PATH configuré)")
        return stat_model

    # ── Cas 2 : poids nul → forcer statistique seul ───────────────────────────
    if model_weight <= 0.0:
        log.info("ModelRegistry: StatisticalModel (MODEL_WEIGHT=0)")
        return stat_model

    # ── Cas 3 : modèle ML depuis fichier ─────────────────────────────────────
    ml_model: Optional[WeatherModel] = None

    if model_path:
        path = Path(model_path)
        if not path.exists():
            log.warning(f"ModelRegistry: MODEL_PATH introuvable : {model_path}")
        elif path.suffix == ".onnx":
            ml_model = OnnxModel(model_path)
        elif path.suffix in (".pkl", ".joblib"):
            ml_model = SklearnModel(model_path)
        else:
            log.warning(f"ModelRegistry: extension inconnue {path.suffix} — skip")

    elif model_endpoint:
        ml_model = RemoteModel(model_endpoint, api_key=model_api_key)

    if ml_model is None or not ml_model.is_available():
        log.warning("ModelRegistry: modèle ML non disponible → fallback StatisticalModel")
        return stat_model

    # ── Cas 4 : poids total → modèle ML seul ─────────────────────────────────
    if model_weight >= 1.0:
        log.info(f"ModelRegistry: {ml_model.name} seul (MODEL_WEIGHT=1.0)")
        return ml_model

    # ── Cas 5 : ensemble Statistical + ML ────────────────────────────────────
    stat_weight = round(1.0 - model_weight, 3)
    ensemble = EnsembleModel([
        (stat_model, stat_weight),
        (ml_model,   model_weight),
    ])
    log.info(
        f"ModelRegistry: EnsembleModel | "
        f"statistical×{stat_weight:.2f} + {ml_model.name}×{model_weight:.2f}"
    )
    return ensemble


def get_model() -> WeatherModel:
    """Retourne le modèle actif (chargé une fois, mis en cache)."""
    global _model
    with _lock:
        if _model is None:
            _model = _build_model()
        return _model


def reload_model() -> WeatherModel:
    """
    Recharge le modèle depuis la configuration actuelle.
    Hot-reload sans redémarrer le bot — utile après un ré-entraînement.
    """
    global _model
    with _lock:
        log.info("ModelRegistry: rechargement du modèle...")
        _model = _build_model()
        return _model


def predict(features: dict) -> Optional[ModelPrediction]:
    """
    Point d'entrée principal — prédit P(Yes) avec le modèle actif.
    Retourne None si le modèle ne peut pas prédire.
    """
    return get_model().predict(features)


def model_info() -> dict:
    """Retourne les infos du modèle actif pour les logs et le monitoring."""
    model = get_model()
    return {
        "name":      model.name,
        "available": model.is_available(),
        "type":      type(model).__name__,
        "model_path":     os.getenv("MODEL_PATH", ""),
        "model_endpoint": os.getenv("MODEL_ENDPOINT", ""),
        "model_weight":   float(os.getenv("MODEL_WEIGHT", "0.6")),
    }


def print_model_info() -> None:
    info = model_info()
    print(f"\n{'═'*60}")
    print(f"  MODÈLE ACTIF")
    print(f"{'═'*60}")
    print(f"  Nom       : {info['name']}")
    print(f"  Type      : {info['type']}")
    print(f"  Disponible: {'✅' if info['available'] else '❌'}")
    if info["model_path"]:
        print(f"  Fichier   : {info['model_path']}")
    if info["model_endpoint"]:
        print(f"  Endpoint  : {info['model_endpoint']}")
    print(f"  Poids ML  : {info['model_weight']:.0%}")
    print(f"{'═'*60}")


if __name__ == "__main__":
    print_model_info()
    # Test de prédiction avec features factices
    test_features = {
        "p_forecast": 0.15,
        "p_climate":  0.20,
        "p_trend":    0.18,
        "forecast_consensus": 12.5,
        "forecast_confidence": 0.85,
        "threshold_value": 15.0,
        "market_yes_prob": 0.40,
        "days_to_expiry": 2,
        "month": 3,
    }
    pred = predict(test_features)
    if pred:
        print(f"\n  Test prédiction : p_yes={pred.p_yes:.3f} conf={pred.confidence:.2f} source={pred.source}")