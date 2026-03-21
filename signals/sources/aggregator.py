"""
Agrégateur météo multi-sources.
Fusionne Open-Meteo + NOAA + WeatherAPI + Tomorrow.io + OWM
en une prévision unique avec score de confiance.

Logique de fusion :
- Moyenne pondérée des sources disponibles
- Poids selon fiabilité historique et freshnessno
- Confiance = f(nb sources, écart-type entre sources)
"""
from __future__ import annotations
import sys, os, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from typing import Optional
from signals.sources import open_meteo, noaa, weatherapi, tomorrow_io

# Poids par source (somme = 1.0 si toutes disponibles)
SOURCE_WEIGHTS = {
    "tomorrow_io":  0.35,   # Meilleure précision technique
    "open_meteo":   0.30,   # Agrège ECMWF + GFS, très fiable
    "noaa":         0.20,   # Officiel US, ultra-fiable pour villes US
    "weatherapi":   0.10,   # Bon complément mondial
    "owm":          0.05,   # Moins précis, en dernier recours
}


def _weighted_mean(values: dict[str, float]) -> tuple[float, float]:
    """
    Retourne (moyenne_pondérée, confiance).
    confiance = 1 - (écart_type / moyenne) normalisé
    """
    total_weight = 0.0
    weighted_sum = 0.0
    vals = []

    for source, val in values.items():
        w = SOURCE_WEIGHTS.get(source, 0.05)
        weighted_sum += val * w
        total_weight += w
        vals.append(val)

    if total_weight == 0:
        return 0.0, 0.0

    mean = weighted_sum / total_weight

    # Écart-type entre sources = mesure de désaccord
    if len(vals) > 1:
        variance = sum((v - mean) ** 2 for v in vals) / len(vals)
        std_dev  = math.sqrt(variance)
        # Confiance : plus l'écart est faible, plus on est sûrs
        # Normalisé : ±3°C d'écart = confiance 0.0
        confidence = max(0.0, min(1.0, 1.0 - (std_dev / 3.0)))
    else:
        confidence = 0.5  # Une seule source = confiance modérée

    # Bonus confiance selon nb de sources
    n = len(vals)
    if n >= 3:
        confidence = min(1.0, confidence + 0.15)
    elif n >= 2:
        confidence = min(1.0, confidence + 0.05)

    return round(mean, 2), round(confidence, 3)


def get_max_temp_consensus(city: str, date_str: str) -> tuple[Optional[float], float, dict]:
    """
    Retourne (temp_consensus, confiance, detail_par_source).
    Appelle toutes les sources disponibles en parallèle.
    """
    results: dict[str, float] = {}

    # Open-Meteo (toujours disponible)
    t = open_meteo.get_max_temp(city, date_str)
    if t is not None:
        results["open_meteo"] = t

    # NOAA (US seulement)
    if noaa.is_us_city(city):
        t = noaa.get_max_temp(city, date_str)
        if t is not None:
            results["noaa"] = t

    # WeatherAPI (si clé disponible)
    t = weatherapi.get_max_temp(city, date_str)
    if t is not None:
        results["weatherapi"] = t

# Tomorrow.io (si clé disponible)
    try:
        t = tomorrow_io.get_max_temp(city, date_str)
        if t is not None:
            results["tomorrow_io"] = t
    except Exception:
        pass

    # OWM (si clé disponible)
    try:
        from signals.weather_fetcher import get_max_temp_for_date
        t = get_max_temp_for_date(city, date_str)
        if t is not None:
            results["owm"] = t
    except Exception:
        pass

    if not results:
        return None, 0.0, {}

    mean, confidence = _weighted_mean(results)
    return mean, confidence, results


def get_precip_consensus(city: str, date_str: str) -> tuple[Optional[float], float, dict]:
    """Précipitations consensuelles (mm) + confiance."""
    results: dict[str, float] = {}

    t = open_meteo.get_precip_mm(city, date_str)
    if t is not None:
        results["open_meteo"] = t

    t = weatherapi.get_precip_mm(city, date_str)
    if t is not None:
        results["weatherapi"] = t

    try:
        t = tomorrow_io.get_precip_mm(city, date_str)
        if t is not None:
            results["tomorrow_io"] = t
    except Exception:
        pass

    if not results:
        return None, 0.0, {}

    mean, confidence = _weighted_mean(results)
    return mean, confidence, results


if __name__ == "__main__":
    from datetime import datetime, timedelta, timezone
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    cities = ["Buenos Aires", "Ankara", "Chicago", "Madrid", "Singapore"]
    print(f"🌍 Consensus météo multi-sources — {tomorrow}\n")
    print(f"{'Ville':<20} {'Temp°C':>8} {'Confiance':>10}  Sources")
    print("─" * 65)
    for city in cities:
        temp, conf, detail = get_max_temp_consensus(city, tomorrow)
        sources_str = "  ".join(f"{k}={v:.1f}°C" for k, v in detail.items())
        temp_str = f"{temp:.1f}°C" if temp is not None else "N/A"
        conf_str = f"{conf*100:.0f}%"
        print(f"{city:<20} {temp_str:>8} {conf_str:>10}  {sources_str}")