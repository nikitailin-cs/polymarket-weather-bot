"""
Open-Meteo — API météo gratuite, sans clé, haute précision.
https://open-meteo.com/
Modèles utilisés : Best Match (agrège automatiquement les meilleurs modèles locaux)
"""
from __future__ import annotations
import json, urllib.request, urllib.parse
from typing import Optional

GEOCODE_URL  = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Cache géocodage pour éviter les appels répétés
_geo_cache: dict[str, Optional[tuple[float, float]]] = {}


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def geocode(city: str) -> Optional[tuple[float, float]]:
    """Retourne (lat, lon) pour une ville, avec cache."""
    if city in _geo_cache:
        return _geo_cache[city]
    try:
        url = f"{GEOCODE_URL}?name={urllib.parse.quote(city)}&count=1&language=en"
        data = _get(url)
        results = data.get("results")
        if not results:
            _geo_cache[city] = None
            return None
        lat = results[0]["latitude"]
        lon = results[0]["longitude"]
        _geo_cache[city] = (lat, lon)
        return (lat, lon)
    except Exception as e:
        print(f"  ⚠️ Open-Meteo geocode [{city}]: {e}")
        _geo_cache[city] = None
        return None


def get_max_temp(city: str, date_str: str) -> Optional[float]:
    """
    Température max prévue (°C) pour une ville à une date YYYY-MM-DD.
    Utilise les données daily de Open-Meteo.
    """
    coords = geocode(city)
    if coords is None:
        return None
    lat, lon = coords
    try:
        url = (
            f"{FORECAST_URL}"
            f"?latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max"
            f"&timezone=auto"
            f"&start_date={date_str}&end_date={date_str}"
        )
        data = _get(url)
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        return float(temps[0]) if temps else None
    except Exception as e:
        print(f"  ⚠️ Open-Meteo temp [{city}]: {e}")
        return None


def get_min_temp(city: str, date_str: str) -> Optional[float]:
    """Température min prévue (°C)."""
    coords = geocode(city)
    if coords is None:
        return None
    lat, lon = coords
    try:
        url = (
            f"{FORECAST_URL}"
            f"?latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_min"
            f"&timezone=auto"
            f"&start_date={date_str}&end_date={date_str}"
        )
        data = _get(url)
        temps = data.get("daily", {}).get("temperature_2m_min", [])
        return float(temps[0]) if temps else None
    except Exception as e:
        print(f"  ⚠️ Open-Meteo min_temp [{city}]: {e}")
        return None


def get_precip_mm(city: str, date_str: str) -> Optional[float]:
    """Précipitations totales (mm) prévues."""
    coords = geocode(city)
    if coords is None:
        return None
    lat, lon = coords
    try:
        url = (
            f"{FORECAST_URL}"
            f"?latitude={lat}&longitude={lon}"
            f"&daily=precipitation_sum"
            f"&timezone=auto"
            f"&start_date={date_str}&end_date={date_str}"
        )
        data = _get(url)
        vals = data.get("daily", {}).get("precipitation_sum", [])
        return float(vals[0]) if vals else None
    except Exception as e:
        print(f"  ⚠️ Open-Meteo precip [{city}]: {e}")
        return None


def get_wind_max(city: str, date_str: str) -> Optional[float]:
    """Vitesse max du vent (km/h) prévue."""
    coords = geocode(city)
    if coords is None:
        return None
    lat, lon = coords
    try:
        url = (
            f"{FORECAST_URL}"
            f"?latitude={lat}&longitude={lon}"
            f"&daily=wind_speed_10m_max"
            f"&timezone=auto"
            f"&start_date={date_str}&end_date={date_str}"
        )
        data = _get(url)
        vals = data.get("daily", {}).get("wind_speed_10m_max", [])
        return float(vals[0]) if vals else None
    except Exception as e:
        print(f"  ⚠️ Open-Meteo wind [{city}]: {e}")
        return None


if __name__ == "__main__":
    # Test immédiat — aucune clé requise
    tests = [
        ("Buenos Aires", "2026-03-21"),
        ("Ankara",       "2026-03-20"),
        ("Chicago",      "2026-03-23"),
        ("Madrid",       "2026-03-23"),
        ("Singapore",    "2026-03-23"),
    ]
    print("🌤️  Test Open-Meteo (sans clé)\n")
    for city, date in tests:
        tmax  = get_max_temp(city, date)
        precip = get_precip_mm(city, date)
        wind  = get_wind_max(city, date)
        print(f"  {city:<20} {date}  max={tmax}°C  precip={precip}mm  wind={wind}km/h")