"""
Moteur de probabilité complet — v2.

Intègre 4 couches de signal :
  1. Prévisions météo multi-sources (Open-Meteo, NOAA, OWM, WeatherAPI)
  2. Climatologie historique 15 ans (distribution réelle)
  3. Tendance récente 7 jours (momentum)
  4. Correction de biais de modèle (calibration auto)

Formule finale :
  P_final = w1 * P_forecast + w2 * P_climate + w3 * P_trend_adjusted
  avec sigma ajusté par la confiance et le biais corrigé
"""
from __future__ import annotations
import re, math, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict
from core.forecast_model_router import forecast_model_routing_enabled
from signals.sources.aggregator import (
    get_max_temp_consensus,
    get_max_temp_contract_aligned,
    get_max_temp_model_routed,
    get_precip_contract_aligned,
    get_precip_consensus,
)
from signals.sources.climate_history import (
    get_climate_stats,
    climate_prob_above,
    climate_prob_exact,
    get_recent_trend,
)
from signals.sources.model_calibrator import (
    get_bias_correction,
    record_prediction,
)
from signals.model_registry import predict as model_predict
from signals.feature_store import (
    record_features, build_feature_vector_from_detail,
)

# ── Paramètres ────────────────────────────────────────────────────────────────

# Incertitude de base des prévisions météo par horizon (°C)
TEMP_SIGMA_BY_DAYS = {0: 1.0, 1: 1.5, 2: 2.0, 3: 2.5, 4: 3.0, 5: 3.5}

# Poids de fusion des 3 signaux
W_FORECAST  = 0.55   # Prévisions météo (signal le plus fort à court terme)
W_CLIMATE   = 0.30   # Climatologie historique
W_TREND     = 0.15   # Tendance récente

# Seuils
MIN_CONFIDENCE  = 0.35   # Confiance minimale pour trader
MIN_CLIMATE_N   = 10     # Observations historiques minimales


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """P(X <= x) pour X ~ N(mu, sigma²)"""
    z = (x - mu) / (sigma * math.sqrt(2))
    return 0.5 * (1 + math.erf(z))


def _prob_exact(target: float, forecast: float, sigma: float,
                tolerance: float = 0.5) -> float:
    """P(forecast ∈ [target-tol, target+tol])"""
    return (
        _normal_cdf(target + tolerance, forecast, sigma)
        - _normal_cdf(target - tolerance, forecast, sigma)
    )


def _days_until(date_str: str) -> int:
    target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return max(0, (target - datetime.now(timezone.utc)).days)


def _get_sigma(days: int, confidence: float) -> float:
    """Sigma ajusté par l'horizon et la confiance des sources."""
    base = TEMP_SIGMA_BY_DAYS.get(min(days, 5), 3.5)
    # Moins de confiance = plus d'incertitude
    return base * (1.0 + (1.0 - confidence) * 0.4)


# ── Parsing des questions ─────────────────────────────────────────────────────

def parse_temp_question(question: str) -> Optional[Dict]:
    """
    Parse une question de marché température.
    Retourne un dict avec city, target_c, date_str, modifier, month.
    """
    m = re.search(
        r"(?:highest|lowest)?\s*temperature in ([A-Za-z\s]+?) be "
        r"([-+]?\d+(?:\.\d+)?)\s*°([CF])"
        r"(\s+or\s+(?:below|above|higher|lower))?",
        question, re.IGNORECASE
    )
    if not m:
        return None

    city     = m.group(1).strip()
    temp     = float(m.group(2))
    unit     = m.group(3).upper()
    modifier = (m.group(4) or "").strip().lower()

    # Conversion °F → °C
    if unit == "F":
        temp = (temp - 32) * 5 / 9

    # Modifier
    if "below" in modifier or "lower" in modifier:
        mod = "below"
    elif "above" in modifier or "higher" in modifier:
        mod = "above"
    else:
        mod = "exact"

    # Date
    _MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
    date_m = re.search(rf"on\s+({_MONTHS})\s+(\d+)", question, re.IGNORECASE)
    if not date_m:
        return None
    try:
        dt       = datetime.strptime(f"{date_m.group(1)} {date_m.group(2)} 2026", "%B %d %Y")
        date_str = dt.strftime("%Y-%m-%d")
        month    = dt.month
    except ValueError:
        return None

    return {
        "city":     city,
        "target_c": round(temp, 2),
        "date_str": date_str,
        "modifier": mod,
        "month":    month,
    }


def parse_precip_question(question: str) -> Optional[Dict]:
    """
    Parse une question de marché précipitations.
    Retourne un dict avec city, target_mm, date_str, modifier, month.
    """
    m = re.search(
        r"(?:precipitation|rain(?:fall)?|snow(?:fall)?) in ([A-Za-z\s]+?) be "
        r"(\d+(?:\.\d+)?)\s*(mm|cm|inches|inch|in)\b"
        r"(\s+or\s+(?:below|above|higher|lower))?",
        question, re.IGNORECASE
    )
    if not m:
        return None

    city     = m.group(1).strip()
    amount   = float(m.group(2))
    unit     = m.group(3).lower()
    modifier = (m.group(4) or "").strip().lower()

    # Conversion vers mm
    if unit == "cm":
        amount *= 10.0
    elif unit in ("in", "inch", "inches"):
        amount *= 25.4

    if "below" in modifier or "lower" in modifier:
        mod = "below"
    elif "above" in modifier or "higher" in modifier:
        mod = "above"
    else:
        mod = "exact"

    _MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
    date_m = re.search(rf"on\s+({_MONTHS})\s+(\d+)", question, re.IGNORECASE)
    if not date_m:
        return None
    try:
        dt       = datetime.strptime(f"{date_m.group(1)} {date_m.group(2)} 2026", "%B %d %Y")
        date_str = dt.strftime("%Y-%m-%d")
        month    = dt.month
    except ValueError:
        return None

    return {
        "city":      city,
        "target_mm": round(amount, 2),
        "date_str":  date_str,
        "modifier":  mod,
        "month":     month,
    }


# ── Calcul de probabilité par couche ─────────────────────────────────────────

def _prob_from_forecast(target_c: float, modifier: str,
                        forecast_c: float, sigma: float) -> float:
    """Probabilité depuis les prévisions météo."""
    if modifier == "below":
        return _normal_cdf(target_c, forecast_c, sigma)
    elif modifier == "above":
        return 1.0 - _normal_cdf(target_c, forecast_c, sigma)
    else:
        return _prob_exact(target_c, forecast_c, sigma)


def _prob_from_forecast_precip(target_mm: float, modifier: str,
                               forecast_mm: float, sigma_mm: float) -> float:
    """Probabilité depuis les prévisions de précipitations."""
    if modifier == "below":
        return _normal_cdf(target_mm, forecast_mm, sigma_mm)
    elif modifier == "above":
        return 1.0 - _normal_cdf(target_mm, forecast_mm, sigma_mm)
    else:
        # Précipitations souvent discrètes: tolérance un peu plus large.
        return _prob_exact(target_mm, forecast_mm, sigma_mm, tolerance=1.0)


def _prob_from_climate(city: str, month: int, target_c: float,
                       modifier: str) -> Optional[float]:
    """Probabilité depuis l'historique climatologique."""
    if modifier == "above":
        return climate_prob_above(city, month, target_c)
    elif modifier == "below":
        p_above = climate_prob_above(city, month, target_c)
        return (1.0 - p_above) if p_above is not None else None
    else:
        return climate_prob_exact(city, month, target_c)


def _prob_from_trend(target_c: float, modifier: str,
                     forecast_c: float, sigma: float,
                     city: str) -> Optional[float]:
    """
    Ajuste la probabilité selon la tendance des 7 derniers jours.
    Si la tendance est à la hausse (+0.5°C/j), on décale la distribution.
    """
    trend = get_recent_trend(city, days=7)
    if trend is None:
        return None

    # Température ajustée par la tendance (projection 1-2 jours)
    days_ahead  = 1
    trend_delta = trend["slope_per_day"] * days_ahead
    adjusted_forecast = forecast_c + trend_delta * 0.3  # Poids modéré

    if modifier == "below":
        return _normal_cdf(target_c, adjusted_forecast, sigma)
    elif modifier == "above":
        return 1.0 - _normal_cdf(target_c, adjusted_forecast, sigma)
    else:
        return _prob_exact(target_c, adjusted_forecast, sigma)


# ── Point d'entrée principal ──────────────────────────────────────────────────

def compute_yes_prob(
    question:          str,
    record:            bool  = False,
    condition_id:      str   = "",
    market_yes_prob:   float = 0.5,
    side:              str   = "NO",
    kelly_stake_usd:   float = 0.0,
    oracle_alignment:  float = 0.0,
    resolution_source: str   = "uma",
    sources_detail:    Optional[dict] = None,
    spec               = None,
) -> Optional[Tuple[float, float, Dict]]:
    """
    Calcule P(Yes) pour un marché météo.

    Retourne (p_yes, confiance, detail) ou None si non supporté.
    detail = dict avec les probabilités par couche pour debug.

    record=True : enregistre la prédiction pour calibration future.
    """
    q = question.lower()

    if "temperature" in q:
        parsed = parse_temp_question(question)
        if not parsed:
            return None

        city      = parsed["city"]
        target_c  = parsed["target_c"]
        date_str  = parsed["date_str"]
        modifier  = parsed["modifier"]
        month     = parsed["month"]

        # ── Couche 1 : prévisions météo ───────────────────────────────────────
        # Si une source oracle contractuelle est connue, on l'utilise en priorité.
        contract_source = ""
        if spec is not None:
            contract_source = str(getattr(spec, "oracle_source", "") or "")

        if contract_source:
            forecast_c, forecast_conf, sources = get_max_temp_contract_aligned(
                city, date_str, contract_source
            )
            # Source inconnue/non supportée => ne pas trader à l'aveugle.
            if forecast_c is None:
                return None
        elif forecast_model_routing_enabled():
            forecast_c, forecast_conf, sources = get_max_temp_model_routed(city, date_str)
        else:
            forecast_c, forecast_conf, sources = get_max_temp_consensus(city, date_str)

        if forecast_c is None or forecast_conf < MIN_CONFIDENCE:
            return None

        # Correction de biais
        bias_corr = get_bias_correction(city) or 0.0
        corrected_forecast = forecast_c + bias_corr

        days  = _days_until(date_str)
        sigma = _get_sigma(days, forecast_conf)

        p_forecast = _prob_from_forecast(target_c, modifier, corrected_forecast, sigma)

        # ── Couche 2 : climatologie historique ────────────────────────────────
        p_climate = _prob_from_climate(city, month, target_c, modifier)

        # ── Couche 3 : tendance récente ───────────────────────────────────────
        p_trend = _prob_from_trend(target_c, modifier, corrected_forecast,
                                   sigma, city)

        # ── Fusion pondérée ───────────────────────────────────────────────────
        if p_climate is not None and p_trend is not None:
            p_final = (W_FORECAST * p_forecast
                     + W_CLIMATE  * p_climate
                     + W_TREND    * p_trend)
            layers_used = 3
        elif p_climate is not None:
            w1 = W_FORECAST / (W_FORECAST + W_CLIMATE)
            w2 = W_CLIMATE  / (W_FORECAST + W_CLIMATE)
            p_final = w1 * p_forecast + w2 * p_climate
            layers_used = 2
        else:
            p_final = p_forecast
            layers_used = 1

        p_final = round(max(0.02, min(0.98, p_final)), 4)

        # Confiance finale : bonus selon nb de couches
        confidence = forecast_conf
        if layers_used >= 3:
            confidence = min(1.0, confidence + 0.10)
        elif layers_used == 2:
            confidence = min(1.0, confidence + 0.05)

        # ── Couche 4 : modèle ML externe (via model_registry) ────────────────
        p_model = None
        try:
            ml_features = {
                "p_forecast":          round(p_forecast, 4),
                "p_climate":           round(p_climate, 4) if p_climate else None,
                "p_trend":             round(p_trend, 4) if p_trend else None,
                "forecast_consensus":  round(corrected_forecast, 2),
                "forecast_confidence": round(forecast_conf, 3),
                "forecast_n_sources":  len(sources),
                "bias_correction":     round(bias_corr, 2),
                "corrected_forecast":  round(corrected_forecast, 2),
                "sigma":               round(sigma, 2),
                "threshold_value":     target_c,
                "days_to_expiry":      days,
                "month":               month,
                "market_yes_prob":     market_yes_prob,
                "oracle_alignment":    oracle_alignment,
            }
            if sources:
                ml_features.update({f"forecast_{k}": v for k, v in sources.items()})
            ml_pred = model_predict(ml_features)
            if ml_pred is not None:
                p_model    = ml_pred.p_yes
                p_final    = ml_pred.p_yes
                confidence = ml_pred.confidence
        except Exception:
            pass   # non-bloquant — garde la fusion statistique

        # ── Enregistrement pour calibration ───────────────────────────────────
        if record:
            record_prediction(city, date_str, corrected_forecast)

        detail = {
            "forecast_c":    round(corrected_forecast, 2),
            "bias_corr":     round(bias_corr, 2),
            "sigma":         round(sigma, 2),
            "p_forecast":    round(p_forecast, 4),
            "p_climate":     round(p_climate, 4) if p_climate else None,
            "p_trend":       round(p_trend, 4) if p_trend else None,
            "p_model":       round(p_model, 4) if p_model is not None else None,
            "p_final":       p_final,
            "layers_used":   layers_used,
            "sources":       list(sources.keys()),
            "days_to_expiry": days,
        }

        # ── Feature store ─────────────────────────────────────────────────────
        if condition_id:
            try:
                fv = build_feature_vector_from_detail(
                    condition_id      = condition_id,
                    question          = question,
                    city              = city,
                    date_str          = date_str,
                    detail            = detail,
                    market_yes_prob   = market_yes_prob,
                    side              = side,
                    kelly_stake_usd   = kelly_stake_usd,
                    oracle_alignment  = oracle_alignment,
                    resolution_source = resolution_source,
                    sources_detail    = sources,
                    spec              = spec,
                )
                record_features(fv)
            except Exception:
                pass   # non-bloquant

        return p_final, round(confidence, 3), detail

    if any(k in q for k in ("precipitation", "rain", "rainfall", "snow", "snowfall")):
        parsed = parse_precip_question(question)
        if not parsed:
            return None

        city       = parsed["city"]
        target_mm  = parsed["target_mm"]
        date_str   = parsed["date_str"]
        modifier   = parsed["modifier"]
        month      = parsed["month"]

        contract_source = ""
        if spec is not None:
            contract_source = str(getattr(spec, "oracle_source", "") or "")
        if contract_source:
            forecast_mm, forecast_conf, sources = get_precip_contract_aligned(
                city, date_str, contract_source
            )
            if forecast_mm is None:
                return None
        else:
            forecast_mm, forecast_conf, sources = get_precip_consensus(city, date_str)
        if forecast_mm is None or forecast_conf < MIN_CONFIDENCE:
            return None

        # Incertitude pluie/neige plus élevée que température (en mm).
        days = _days_until(date_str)
        sigma_mm = 2.0 + min(days, 5) * 0.8
        sigma_mm *= (1.0 + (1.0 - forecast_conf) * 0.5)

        p_forecast = _prob_from_forecast_precip(target_mm, modifier, forecast_mm, sigma_mm)

        # Fallback climatologie (pas encore implémentée pour precip):
        # on garde la fusion simple basée sur la prévision.
        p_final = round(max(0.02, min(0.98, p_forecast)), 4)

        confidence = min(1.0, forecast_conf + 0.02)
        detail = {
            "forecast_mm":     round(forecast_mm, 2),
            "sigma_mm":        round(sigma_mm, 2),
            "p_forecast":      round(p_forecast, 4),
            "p_final":         p_final,
            "layers_used":     1,
            "sources":         list(sources.keys()),
            "days_to_expiry":  days,
            "domain":          "precipitation",
            "month":           month,
        }
        return p_final, round(confidence, 3), detail

    return None


if __name__ == "__main__":
    tests = [
        "Will the highest temperature in Buenos Aires be 29°C on March 21?",
        "Will the highest temperature in Ankara be 7°C on March 20?",
        "Will the highest temperature in Chicago be 35°F or below on March 23?",
        "Will the highest temperature in Madrid be 21°C on March 23?",
        "Will the highest temperature in Seoul be 16°C or higher on March 23?",
        "Will the highest temperature in Munich be 15°C on March 23?",
    ]

    print("🧠 Probability Engine v2 — Multi-couches\n")
    print(f"{'Question':<60} {'P(Yes)':>7} {'Conf':>6} {'Layers':>7}")
    print("─" * 82)

    for q in tests:
        result = compute_yes_prob(q)
        if result:
            p, conf, detail = result
            layers = detail["layers_used"]
            fc     = detail["forecast_c"]
            bias   = detail["bias_corr"]
            print(f"{q[:58]:<60} {p*100:>6.1f}% {conf*100:>5.0f}%  {layers}/3")
            p_clim_str  = f"{detail['p_climate']*100:.1f}%" if detail['p_climate'] else 'N/A'
            p_trend_str = f"{detail['p_trend']*100:.1f}%"   if detail['p_trend']   else 'N/A'
            print(f"  → prév={fc}°C  biais={bias:+.2f}°C  "
                  f"P_fc={detail['p_forecast']*100:.1f}%  "
                  f"P_clim={p_clim_str:>6}  "
                  f"P_trend={p_trend_str:>6}")
            print()
        else:
            print(f"{q[:58]:<60} N/A")