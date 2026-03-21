"""
Tomorrow.io — API météo premium, plan académique UCSD gratuit.
https://www.tomorrow.io/
Inscription académique : tomorrow.io/weather-api/ → "For Research"
Ajouter dans .env : TOMORROW_API_KEY=ta_cle

Avantages vs autres sources :
- Résolution temporelle : 1h (vs 3h OWM)
- Modèles propriétaires + satellite en temps réel
- Nowcasting précis à 1-6h
- Données historiques incluses
"""
from __future__ import annotations
import json, urllib.request, urllib.parse, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from typing import Optional

TIMELINE_URL  = "https://api.tomorrow.io/v4/timelines"
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
TOMORROW_KEY = os.getenv("TOMORROW_API_KEY", "")
# Cache géocodage Tomorrow (city → lat/lon via Open-Meteo pour éviter les appels)
_geo_cache: dict[str, Optional[tuple[float, float]]] = {}


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _geocode(city: str) -> Optional[tuple[float, float]]:
    """Géocode via Open-Meteo (gratuit, pas de clé supplémentaire)."""
    if city in _geo_cache:
        return _geo_cache[city]
    try:
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=1"
        data = _get(url)
        results = data.get("results")
        if not results:
            _geo_cache[city] = None
            return None
        coords = (results[0]["latitude"], results[0]["longitude"])
        _geo_cache[city] = coords
        return coords
    except Exception:
        _geo_cache[city] = None
        return None


def _get_daily(city: str, date_str: str, fields: list[str]) -> Optional[dict]:
    if not TOMORROW_KEY:
        return None
    coords = _geocode(city)
    if coords is None:
        return None
    lat, lon = coords
    try:
        fields_str = urllib.parse.quote(",".join(fields))
        url = (
            f"https://api.tomorrow.io/v4/weather/forecast"
            f"?location={lat},{lon}"
            f"&apikey={TOMORROW_KEY}"
        )
        data = _get(url)
        for day in data.get("timelines", {}).get("daily", []):
            if (day.get("time") or "")[:10] == date_str:
                return day.get("values", {})
        return None
    except Exception:
        return None


def get_max_temp(city: str, date_str: str) -> Optional[float]:
    """Température max prévue (°C)."""
    vals = _get_daily(city, date_str, ["temperatureMax"])
    return vals.get("temperatureMax") if vals else None


def get_min_temp(city: str, date_str: str) -> Optional[float]:
    """Température min prévue (°C)."""
    vals = _get_daily(city, date_str, ["temperatureMin"])
    return vals.get("temperatureMin") if vals else None


def get_precip_mm(city: str, date_str: str) -> Optional[float]:
    vals = _get_daily(city, date_str, ["precipitationAccumulation"])
    if vals is None:
        return None
    # Nouveaux noms de champs API v4
    return (vals.get("precipitationAccumulation")
         or vals.get("rainAccumulation")
         or vals.get("snowAccumulation"))


def get_wind_max(city: str, date_str: str) -> Optional[float]:
    """Vitesse max du vent (m/s)."""
    vals = _get_daily(city, date_str, ["windSpeedMax"])
    return vals.get("windSpeedMax") if vals else None


def get_precip_probability(city: str, date_str: str) -> Optional[float]:
    vals = _get_daily(city, date_str, ["precipitationProbability"])
    if vals is None:
        return None
    val = vals.get("precipitationProbability")
    return float(val) / 100.0 if val is not None else None


if __name__ == "__main__":
    import time
    if not TOMORROW_KEY:
        print("⚠️  TOMORROW_API_KEY manquante dans .env")
    else:
        from datetime import datetime, timedelta, timezone
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        print(f"🔮 Test Tomorrow.io — {tomorrow}\n")
        for city in ["Buenos Aires", "Ankara", "Chicago", "Madrid", "Tokyo"]:
            tmax  = get_max_temp(city, tomorrow)
            precip = get_precip_mm(city, tomorrow)
            wind  = get_wind_max(city, tomorrow)
            prob  = get_precip_probability(city, tomorrow)
            print(f"  {city:<20} max={tmax}°C  precip={precip}mm  wind={wind}m/s  P(rain)={f'{prob*100:.0f}%' if prob else 'N/A'}")
            time.sleep(1)  # Respecte le rate limit