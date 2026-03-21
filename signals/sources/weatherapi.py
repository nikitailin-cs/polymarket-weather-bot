"""
WeatherAPI.com — 1M calls/mois gratuit, mondial, données riches.
https://www.weatherapi.com/
Clé gratuite sur : weatherapi.com/signup.aspx
Ajouter dans .env : WEATHERAPI_KEY=ta_cle
"""
from __future__ import annotations
import json, urllib.request, urllib.parse, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from typing import Optional

FORECAST_URL = "https://api.weatherapi.com/v1/forecast.json"
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY", "")
# Nombre de jours max gratuit = 3 (plan free), 14 (plan payant)
MAX_FORECAST_DAYS = 3


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _days_from_today(date_str: str) -> int:
    from datetime import datetime, timezone
    target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    now    = datetime.now(timezone.utc)
    return max(1, (target - now).days + 1)


def get_forecast_day(city: str, date_str: str) -> Optional[dict]:
    """Retourne le dict 'day' de WeatherAPI pour une ville/date."""
    if not WEATHERAPI_KEY:
        return None
    days = min(_days_from_today(date_str), MAX_FORECAST_DAYS)
    try:
        url = (
            f"{FORECAST_URL}"
            f"?key={WEATHERAPI_KEY}"
            f"&q={urllib.parse.quote(city)}"
            f"&days={days}"
            f"&aqi=no&alerts=no"
        )
        data = _get(url)
        for fc_day in data.get("forecast", {}).get("forecastday", []):
            if fc_day.get("date") == date_str:
                return fc_day.get("day", {})
        return None
    except Exception as e:
        print(f"  ⚠️ WeatherAPI [{city}]: {e}")
        return None


def get_max_temp(city: str, date_str: str) -> Optional[float]:
    """Température max prévue (°C)."""
    day = get_forecast_day(city, date_str)
    if day is None:
        return None
    return day.get("maxtemp_c")


def get_min_temp(city: str, date_str: str) -> Optional[float]:
    """Température min prévue (°C)."""
    day = get_forecast_day(city, date_str)
    if day is None:
        return None
    return day.get("mintemp_c")


def get_precip_mm(city: str, date_str: str) -> Optional[float]:
    """Précipitations totales prévues (mm)."""
    day = get_forecast_day(city, date_str)
    if day is None:
        return None
    return day.get("totalprecip_mm")


def get_wind_max_kph(city: str, date_str: str) -> Optional[float]:
    """Vitesse max du vent (km/h)."""
    day = get_forecast_day(city, date_str)
    if day is None:
        return None
    return day.get("maxwind_kph")


def get_precip_chance(city: str, date_str: str) -> Optional[float]:
    """Probabilité de pluie (0.0-1.0)."""
    day = get_forecast_day(city, date_str)
    if day is None:
        return None
    val = day.get("daily_chance_of_rain")
    return float(val) / 100.0 if val is not None else None


if __name__ == "__main__":
    if not WEATHERAPI_KEY:
        print("⚠️  WEATHERAPI_KEY manquante dans .env")
        print("   → Inscris-toi sur weatherapi.com/signup.aspx (gratuit)")
        print("   → Ajoute WEATHERAPI_KEY=ta_cle dans .env")
    else:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        print(f"🌦️  Test WeatherAPI — {today}\n")
        for city in ["Buenos Aires", "Ankara", "Chicago", "Madrid", "Singapore", "Tokyo"]:
            tmax   = get_max_temp(city, today)
            precip = get_precip_mm(city, today)
            wind   = get_wind_max_kph(city, today)
            print(f"  {city:<20} max={tmax}°C  precip={precip}mm  wind={wind}km/h")
