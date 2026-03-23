"""
signals/model_interface.py
---------------------------
Interface abstraite pour tout modèle de prédiction météo-marché.

Principe : le pipeline de trading ne connaît que WeatherModel.predict().
L'implémentation (statistique, sklearn, ONNX, API distante) est
transparente et interchangeable sans modifier le code métier.

Implémentations incluses :
  StatisticalModel  — encapsule les 3 couches actuelles du probability_engine
                      (forecast + climatologie + tendance)
  SklearnModel      — charge tout modèle sklearn/joblib (.pkl)
  OnnxModel         — charge tout modèle ONNX (.onnx), via onnxruntime
  EnsembleModel     — combine plusieurs modèles avec poids configurables
  RemoteModel       — délègue à une API HTTP (pour modèles coûteux en GPU)

Ajouter un nouveau modèle :
  1. Hériter de WeatherModel
  2. Implémenter predict(features: dict) -> ModelPrediction
  3. L'enregistrer dans model_registry.py

Format des features en entrée : dict (depuis FeatureVector.to_dict()).
Format de sortie : ModelPrediction avec p_yes, confidence, source, metadata.
"""
from __future__ import annotations

import math
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

log = logging.getLogger(__name__)


# ── Sortie standard ───────────────────────────────────────────────────────────

@dataclass
class ModelPrediction:
    """
    Sortie standardisée de tout modèle.
    Compatible avec le pipeline compute_yes_prob existant.
    """
    p_yes:       float          # probabilité P(Yes) ∈ [0.02, 0.98]
    confidence:  float          # confiance du modèle ∈ [0, 1]
    source:      str            # nom du modèle ("statistical", "xgboost", ...)
    metadata:    Dict[str, Any] = field(default_factory=dict)  # debug/audit

    def clamp(self) -> "ModelPrediction":
        """Clamp p_yes dans [0.02, 0.98] — bornes Polymarket."""
        self.p_yes      = round(max(0.02, min(0.98, self.p_yes)), 4)
        self.confidence = round(max(0.0,  min(1.0, self.confidence)), 3)
        return self


# ── Interface abstraite ───────────────────────────────────────────────────────

class WeatherModel(ABC):
    """
    Interface que tout modèle de prédiction doit implémenter.
    Le modèle reçoit un dict de features (FeatureVector.to_dict())
    et retourne une ModelPrediction.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Identifiant du modèle — utilisé dans les logs et le feature store."""
        ...

    @abstractmethod
    def predict(self, features: dict) -> Optional[ModelPrediction]:
        """
        Calcule P(Yes) pour un marché météo.

        Args:
            features : dict depuis FeatureVector.to_dict()
                       Les champs peuvent être None si non disponibles.

        Returns:
            ModelPrediction ou None si le modèle ne peut pas prédire
            (features insuffisantes, erreur, etc.)
        """
        ...

    def is_available(self) -> bool:
        """Retourne True si le modèle est prêt à prédire."""
        return True


# ── Implémentation 1 : modèle statistique (pipeline actuel encapsulé) ────────

class StatisticalModel(WeatherModel):
    """
    Encapsule les 3 couches actuelles du probability_engine.
    C'est le modèle par défaut — aucune dépendance externe.

    Couches :
      1. Gaussienne centrée sur forecast_consensus (avec sigma ajusté)
      2. Climatologie historique (distribution réelle 15 ans)
      3. Tendance récente 7 jours (momentum)
    """

    @property
    def name(self) -> str:
        return "statistical_v2"

    def predict(self, features: dict) -> Optional[ModelPrediction]:
        try:
            # Récupère les probabilités par couche déjà calculées
            # (elles sont dans les features car calculées en amont)
            p_forecast = features.get("p_forecast")
            p_climate  = features.get("p_climate")
            p_trend    = features.get("p_trend")

            if p_forecast is None:
                return None

            # Poids depuis settings ou défauts
            try:
                from signals.probability_engine import W_FORECAST, W_CLIMATE, W_TREND
            except Exception:
                W_FORECAST, W_CLIMATE, W_TREND = 0.55, 0.30, 0.15

            if p_climate is not None and p_trend is not None:
                p_final    = W_FORECAST * p_forecast + W_CLIMATE * p_climate + W_TREND * p_trend
                layers     = 3
                confidence = min(1.0, features.get("forecast_confidence", 0.5) + 0.10)
            elif p_climate is not None:
                w1 = W_FORECAST / (W_FORECAST + W_CLIMATE)
                w2 = W_CLIMATE  / (W_FORECAST + W_CLIMATE)
                p_final    = w1 * p_forecast + w2 * p_climate
                layers     = 2
                confidence = min(1.0, features.get("forecast_confidence", 0.5) + 0.05)
            else:
                p_final    = p_forecast
                layers     = 1
                confidence = features.get("forecast_confidence", 0.5)

            return ModelPrediction(
                p_yes      = p_final,
                confidence = confidence,
                source     = self.name,
                metadata   = {"layers_used": layers},
            ).clamp()

        except Exception as e:
            log.debug(f"StatisticalModel.predict erreur: {e}")
            return None


# ── Implémentation 2 : modèle sklearn/joblib ─────────────────────────────────

class SklearnModel(WeatherModel):
    """
    Charge tout modèle sérialisé avec joblib (sklearn, XGBoost, LightGBM...).
    Le modèle doit exposer predict_proba(X) → [[p_no, p_yes], ...].

    Format des features en entrée au modèle :
        numpy array de shape (1, N_FEATURES)
        dans l'ordre défini par FEATURE_COLUMNS ci-dessous.

    Entraînement (exemple minimal) :
        from signals.feature_store import load_dataframe
        df = load_dataframe(labeled_only=True)
        X = df[FEATURE_COLUMNS].fillna(-1).values
        y = df["outcome_yes"].astype(int).values
        from sklearn.ensemble import GradientBoostingClassifier
        import joblib
        model = GradientBoostingClassifier(n_estimators=200)
        model.fit(X, y)
        joblib.dump(model, "data/ml/model.pkl")
    """

    # Colonnes utilisées dans cet ordre strict
    FEATURE_COLUMNS = [
        "forecast_consensus", "forecast_confidence", "forecast_n_sources",
        "forecast_std", "bias_correction", "corrected_forecast", "sigma",
        "threshold_value", "days_to_expiry", "month",
        "p_forecast", "p_climate", "p_trend",
        "market_yes_prob", "oracle_alignment",
    ]

    def __init__(self, model_path: str):
        self._path  = model_path
        self._model = None
        self._load()

    def _load(self) -> None:
        try:
            import joblib
            self._model = joblib.load(self._path)
            log.info(f"SklearnModel chargé : {self._path}")
        except Exception as e:
            log.warning(f"SklearnModel impossible à charger [{self._path}]: {e}")
            self._model = None

    @property
    def name(self) -> str:
        from pathlib import Path
        return f"sklearn:{Path(self._path).stem}"

    def is_available(self) -> bool:
        return self._model is not None

    def predict(self, features: dict) -> Optional[ModelPrediction]:
        if self._model is None:
            return None
        try:
            import numpy as np
            x = np.array([[
                features.get(col) if features.get(col) is not None else -1.0
                for col in self.FEATURE_COLUMNS
            ]], dtype=np.float32)

            if hasattr(self._model, "predict_proba"):
                proba   = self._model.predict_proba(x)[0]
                p_yes   = float(proba[1]) if len(proba) > 1 else float(proba[0])
            else:
                # Régression — output direct
                p_yes = float(self._model.predict(x)[0])

            # Confiance = 1 - entropie de la prédiction (max quand p=0 ou 1)
            p_no       = 1.0 - p_yes
            entropy    = -(p_yes * math.log(p_yes + 1e-9) + p_no * math.log(p_no + 1e-9))
            max_entropy = math.log(2)
            confidence  = round(1.0 - entropy / max_entropy, 3)

            return ModelPrediction(
                p_yes      = p_yes,
                confidence = confidence,
                source     = self.name,
                metadata   = {"features_used": self.FEATURE_COLUMNS},
            ).clamp()

        except Exception as e:
            log.warning(f"SklearnModel.predict erreur: {e}")
            return None


# ── Implémentation 3 : modèle ONNX ───────────────────────────────────────────

class OnnxModel(WeatherModel):
    """
    Charge tout modèle au format ONNX (PyTorch, TensorFlow, sklearn...).
    Compatible avec onnxruntime (déjà disponible dans requirements).

    Export depuis sklearn :
        from skl2onnx import convert_sklearn
        from skl2onnx.common.data_types import FloatTensorType
        onnx_model = convert_sklearn(model, "WeatherModel",
                                     [("input", FloatTensorType([None, N]))])
        with open("data/ml/model.onnx", "wb") as f:
            f.write(onnx_model.SerializeToString())

    Export depuis PyTorch :
        torch.onnx.export(model, dummy_input, "data/ml/model.onnx",
                          input_names=["input"], output_names=["output"])
    """

    FEATURE_COLUMNS = SklearnModel.FEATURE_COLUMNS   # même ordre

    def __init__(self, model_path: str):
        self._path    = model_path
        self._session = None
        self._load()

    def _load(self) -> None:
        try:
            import onnxruntime as rt
            self._session = rt.InferenceSession(
                self._path,
                providers=["CPUExecutionProvider"],
            )
            log.info(f"OnnxModel chargé : {self._path}")
        except Exception as e:
            log.warning(f"OnnxModel impossible à charger [{self._path}]: {e}")
            self._session = None

    @property
    def name(self) -> str:
        from pathlib import Path
        return f"onnx:{Path(self._path).stem}"

    def is_available(self) -> bool:
        return self._session is not None

    def predict(self, features: dict) -> Optional[ModelPrediction]:
        if self._session is None:
            return None
        try:
            import numpy as np
            x = np.array([[
                features.get(col) if features.get(col) is not None else -1.0
                for col in self.FEATURE_COLUMNS
            ]], dtype=np.float32)

            input_name  = self._session.get_inputs()[0].name
            output_name = self._session.get_outputs()[0].name
            result      = self._session.run([output_name], {input_name: x})[0]

            # Gère les sorties probas (shape [1,2]) et scalaires (shape [1,1])
            if result.ndim == 2 and result.shape[1] == 2:
                p_yes = float(result[0][1])
            else:
                p_yes = float(result.flat[0])

            p_no       = 1.0 - p_yes
            entropy    = -(p_yes * math.log(p_yes + 1e-9) + p_no * math.log(p_no + 1e-9))
            confidence = round(1.0 - entropy / math.log(2), 3)

            return ModelPrediction(
                p_yes      = p_yes,
                confidence = confidence,
                source     = self.name,
            ).clamp()

        except Exception as e:
            log.warning(f"OnnxModel.predict erreur: {e}")
            return None


# ── Implémentation 4 : ensemble ───────────────────────────────────────────────

class EnsembleModel(WeatherModel):
    """
    Combine plusieurs modèles avec poids configurables.
    Utile pour combiner le modèle statistique avec un modèle ML.

    Exemple :
        ensemble = EnsembleModel([
            (StatisticalModel(),      0.40),
            (SklearnModel("m.pkl"),   0.60),
        ])
    """

    def __init__(self, models_weights: List[tuple]):
        # models_weights = [(model, weight), ...]
        self._models  = models_weights
        total = sum(w for _, w in models_weights)
        self._models  = [(m, w/total) for m, w in models_weights]

    @property
    def name(self) -> str:
        names = "+".join(m.name for m, _ in self._models)
        return f"ensemble:{names}"

    def is_available(self) -> bool:
        return any(m.is_available() for m, _ in self._models)

    def predict(self, features: dict) -> Optional[ModelPrediction]:
        p_weighted   = 0.0
        conf_weighted = 0.0
        total_w      = 0.0
        sources      = []

        for model, weight in self._models:
            if not model.is_available():
                continue
            pred = model.predict(features)
            if pred is None:
                continue
            p_weighted    += pred.p_yes      * weight
            conf_weighted += pred.confidence * weight
            total_w       += weight
            sources.append(f"{model.name}({pred.p_yes:.3f}×{weight:.2f})")

        if total_w == 0:
            return None

        return ModelPrediction(
            p_yes      = p_weighted / total_w,
            confidence = conf_weighted / total_w,
            source     = self.name,
            metadata   = {"components": sources},
        ).clamp()


# ── Implémentation 5 : modèle distant (API HTTP) ─────────────────────────────

class RemoteModel(WeatherModel):
    """
    Délègue la prédiction à une API HTTP externe.
    Utile pour les modèles coûteux en GPU (transformers météo, LLMs).

    Protocol :
        POST {endpoint}/predict
        Body : FeatureVector.to_dict() (JSON)
        Response : {"p_yes": float, "confidence": float, "metadata": {...}}
    """

    def __init__(self, endpoint: str, timeout: int = 5, api_key: str = ""):
        self._endpoint = endpoint.rstrip("/")
        self._timeout  = timeout
        self._api_key  = api_key

    @property
    def name(self) -> str:
        return f"remote:{self._endpoint.split('/')[-1]}"

    def predict(self, features: dict) -> Optional[ModelPrediction]:
        try:
            import urllib.request
            import json as _json

            payload = _json.dumps(features, ensure_ascii=False).encode()
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"

            req  = urllib.request.Request(
                f"{self._endpoint}/predict",
                data=payload, headers=headers,
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                resp = _json.loads(r.read())

            return ModelPrediction(
                p_yes      = float(resp["p_yes"]),
                confidence = float(resp.get("confidence", 0.5)),
                source     = self.name,
                metadata   = resp.get("metadata", {}),
            ).clamp()

        except Exception as e:
            log.warning(f"RemoteModel.predict erreur [{self._endpoint}]: {e}")
            return None