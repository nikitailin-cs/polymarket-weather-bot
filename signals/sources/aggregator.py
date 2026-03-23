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
from typing import Optional, Callable, Dict, Any
from signals.sources import open_meteo, noaa, weatherapi, tomorrow_io

# Poids par source (somme = 1.0 si toutes disponibles)
SOURCE_WEIGHTS = {
    "tomorrow_io":  0.35,   # Meilleure précision technique
    "open_meteo":   0.30,   # Agrège ECMWF + GFS, très fiable
    "noaa":         0.20,   # Officiel US, ultra-fiable pour villes US
    "weatherapi":   0.10,   # Bon complément mondial
    "owm":          0.05,   # Moins précis, en dernier recours
}

ConnectorFn = Callable[[str, str], Optional[float]]


def _none_connector(_city: str, _date_str: str) -> Optional[float]:
    return None


# Registry centralisé des connecteurs contractuels (Étape 3).
# - fetch_temp_max / fetch_precip_mm : fonction de collecte
# - base_confidence: confiance de base de la source
# - proxy: True si approximation via une autre source
CONTRACT_SOURCE_CONNECTORS: Dict[str, Dict[str, Any]] = {
    "noaa_ghcnd": {
        "fetch_temp_max": lambda city, date_str: noaa.get_max_temp(city, date_str),
        "fetch_precip_mm": lambda city, date_str: open_meteo.get_precip_mm(city, date_str),
        "base_confidence": 0.90,
        "proxy": False,
    },
    "wmo_station": {
        "fetch_temp_max": lambda city, date_str: open_meteo.get_max_temp(city, date_str),
        "fetch_precip_mm": lambda city, date_str: open_meteo.get_precip_mm(city, date_str),
        "base_confidence": 0.84,
        "proxy": True,
    },
    "wunderground": {
        "fetch_temp_max": lambda city, date_str: open_meteo.get_max_temp(city, date_str),
        "fetch_precip_mm": lambda city, date_str: open_meteo.get_precip_mm(city, date_str),
        "base_confidence": 0.82,
        "proxy": True,
    },
    "weather_com": {
        "fetch_temp_max": lambda city, date_str: open_meteo.get_max_temp(city, date_str),
        "fetch_precip_mm": lambda city, date_str: open_meteo.get_precip_mm(city, date_str),
        "base_confidence": 0.80,
        "proxy": True,
    },
    "accuweather": {
        "fetch_temp_max": lambda city, date_str: open_meteo.get_max_temp(city, date_str),
        "fetch_precip_mm": lambda city, date_str: open_meteo.get_precip_mm(city, date_str),
        "base_confidence": 0.80,
        "proxy": True,
    },
    "hk_observatory": {
        "fetch_temp_max": lambda city, date_str: open_meteo.get_max_temp(city, date_str),
        "fetch_precip_mm": lambda city, date_str: open_meteo.get_precip_mm(city, date_str),
        "base_confidence": 0.82,
        "proxy": True,
    },
    "meteo_france": {
        "fetch_temp_max": lambda city, date_str: open_meteo.get_max_temp(city, date_str),
        "fetch_precip_mm": lambda city, date_str: open_meteo.get_precip_mm(city, date_str),
        "base_confidence": 0.82,
        "proxy": True,
    },
    "copernicus": {
        "fetch_temp_max": lambda city, date_str: open_meteo.get_max_temp(city, date_str),
        "fetch_precip_mm": lambda city, date_str: open_meteo.get_precip_mm(city, date_str),
        "base_confidence": 0.80,
        "proxy": True,
    },
    # Sources reconnues mais non implémentées localement pour l'instant.
    "nhc": {
        "fetch_temp_max": _none_connector,
        "fetch_precip_mm": _none_connector,
        "base_confidence": 0.0,
        "proxy": False,
    },
    "nasa_giss": {
        "fetch_temp_max": _none_connector,  # marché d'anomalie globale, pas station/city-day.
        "fetch_precip_mm": _none_connector,
        "base_confidence": 0.0,
        "proxy": False,
    },
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


def get_max_temp_contract_aligned(
    city: str,
    date_str: str,
    oracle_source: str,
) -> tuple[Optional[float], float, dict]:
    """
    Prédiction temp alignée sur la source contractuelle.
    Retourne (temp, confiance, details).
    Si la source n'est pas supportée, retourne (None, 0.0, {}).
    """
    source = (oracle_source or "").lower().strip()
    meta = CONTRACT_SOURCE_CONNECTORS.get(source)
    if meta is None:
        return None, 0.0, {"reason": "unknown_source"}
    fetch: ConnectorFn = meta["fetch_temp_max"]
    value = fetch(city, date_str)
    if value is None:
        return None, 0.0, {"reason": "connector_returned_none", "source": source}
    conf = float(meta.get("base_confidence", 0.75))
    return value, conf, {source: value, "_router_proxy": bool(meta.get("proxy", False))}


def get_precip_contract_aligned(
    city: str,
    date_str: str,
    oracle_source: str,
) -> tuple[Optional[float], float, dict]:
    """
    Précipitations alignées sur la source contractuelle.
    """
    source = (oracle_source or "").lower().strip()
    meta = CONTRACT_SOURCE_CONNECTORS.get(source)
    if meta is None:
        return None, 0.0, {"reason": "unknown_source"}
    fetch: ConnectorFn = meta["fetch_precip_mm"]
    value = fetch(city, date_str)
    if value is None:
        return None, 0.0, {"reason": "connector_returned_none", "source": source}
    conf = float(meta.get("base_confidence", 0.75))
    return value, conf, {source: value, "_router_proxy": bool(meta.get("proxy", False))}


def is_contract_source_supported(oracle_source: str, weather_variable: str) -> bool:
    """
    Vérifie si la source contractuelle est routable pour la variable demandée.
    """
    source = (oracle_source or "").lower().strip()
    var = (weather_variable or "").lower().strip()
    meta = CONTRACT_SOURCE_CONNECTORS.get(source)
    if meta is None:
        return False
    if var.startswith("temp"):
        return meta.get("fetch_temp_max") is not _none_connector
    if var in {"precip", "snow"}:
        return meta.get("fetch_precip_mm") is not _none_connector
    return False


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