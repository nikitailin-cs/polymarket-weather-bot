"""
NOAA/NWS — API officielle météo US, sans clé, ultra-fiable.
https://www.weather.gov/documentation/services-web-api
Couverture : États-Unis uniquement.
"""
from __future__ import annotations
import json, urllib.request
from typing import Optional

NWS_POINTS_URL = "https://api.weather.gov/points"
HEADERS = {
    "User-Agent": "PolymarketWeatherBot/1.0 (contact@polybot.com)",
    "Accept": "application/geo+json",
}

# Cache points NWS (lat/lon → gridpoint URL)
_points_cache: dict[str, Optional[str]] = {}

# Coordonnées des villes US couvertes par nos marchés
US_CITIES: dict[str, tuple[float, float]] = {
    "chicago":       (41.8781, -87.6298),
    "seattle":       (47.6062, -122.3321),
    "new york":      (40.7128, -74.0060),
    "los angeles":   (34.0522, -118.2437),
    "miami":         (25.7617, -80.1918),
    "houston":       (29.7604, -95.3698),
    "phoenix":       (33.4484, -112.0740),
    "denver":        (39.7392, -104.9903),
    "boston":        (42.3601, -71.0589),
    "atlanta":       (33.7490, -84.3880),
    "dallas":        (32.7767, -96.7970),
    "san francisco": (37.7749, -122.4194),
}


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _get_forecast_url(city: str) -> Optional[str]:
    """Récupère l'URL de prévision NWS pour une ville US."""
    key = city.lower().strip()
    if key in _points_cache:
        return _points_cache[key]

    coords = US_CITIES.get(key)
    if coords is None:
        _points_cache[key] = None
        return None

    lat, lon = coords
    try:
        data = _get(f"{NWS_POINTS_URL}/{lat},{lon}")
        forecast_url = data.get("properties", {}).get("forecast")
        _points_cache[key] = forecast_url
        return forecast_url
    except Exception as e:
        print(f"  ⚠️ NOAA points [{city}]: {e}")
        _points_cache[key] = None
        return None


def is_us_city(city: str) -> bool:
    return city.lower().strip() in US_CITIES


def get_max_temp(city: str, date_str: str) -> Optional[float]:
    """Température max prévue (°C) pour une ville US."""
    forecast_url = _get_forecast_url(city)
    if not forecast_url:
        return None
    try:
        data = _get(forecast_url)
        periods = data.get("properties", {}).get("periods", [])
        max_temp = None
        for period in periods:
            start = (period.get("startTime") or "")[:10]
            if start != date_str:
                continue
            temp_f = period.get("temperature")
            unit   = period.get("temperatureUnit", "F")
            if temp_f is None:
                continue
            temp_c = (float(temp_f) - 32) * 5 / 9 if unit == "F" else float(temp_f)
            if max_temp is None or temp_c > max_temp:
                max_temp = temp_c
        return round(max_temp, 1) if max_temp is not None else None
    except Exception as e:
        print(f"  ⚠️ NOAA forecast [{city}]: {e}")
        return None


def get_precip_chance(city: str, date_str: str) -> Optional[float]:
    """Probabilité de précipitations (0.0-1.0) pour une ville US."""
    forecast_url = _get_forecast_url(city)
    if not forecast_url:
        return None
    try:
        data = _get(forecast_url)
        periods = data.get("properties", {}).get("periods", [])
        chances = []
        for period in periods:
            start = (period.get("startTime") or "")[:10]
            if start != date_str:
                continue
            pop = period.get("probabilityOfPrecipitation", {})
            val = pop.get("value") if isinstance(pop, dict) else pop
            if val is not None:
                chances.append(float(val) / 100.0)
        return max(chances) if chances else None
    except Exception as e:
        print(f"  ⚠️ NOAA precip [{city}]: {e}")
        return None


if __name__ == "__main__":
    from datetime import datetime, timedelta, timezone
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    print("🇺🇸 Test NOAA/NWS (sans clé)\n")
    for city in ["Chicago", "Seattle", "Miami", "Denver"]:
        tmax   = get_max_temp(city, tomorrow)
        precip = get_precip_chance(city, tomorrow)
        print(f"  {city:<20} {tomorrow}  max={tmax}°C  precip_chance={f'{precip*100:.0f}%' if precip else 'N/A'}")