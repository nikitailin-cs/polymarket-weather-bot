"""
Récupère les prévisions météo via OpenWeatherMap pour une ville donnée.
"""
from __future__ import annotations
import json, urllib.request, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone
from typing import Optional
from config.settings import OWM_API_KEY
import json, urllib.request, urllib.parse, os, sys

OWM_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
OWM_CURRENT_URL  = "https://api.openweathermap.org/data/2.5/weather"

def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def get_forecast(city: str) -> Optional[dict]:
    """Retourne les prévisions 5 jours pour une ville."""
    city_encoded = urllib.parse.quote(city)
    url = f"{OWM_FORECAST_URL}?q={city_encoded}&appid={OWM_API_KEY}&units=metric&cnt=40"
    try:
        return _get(url)
    except Exception as e:
        print(f"  ⚠️ OWM forecast error [{city}]: {e}")
        return None

def get_max_temp_for_date(city: str, target_date: str) -> Optional[float]:
    """
    Retourne la température max prévue pour une ville à une date donnée.
    target_date format : 'YYYY-MM-DD'
    """
    data = get_forecast(city)
    if not data:
        return None
    
    temps = []
    for entry in data.get("list", []):
        dt_txt = entry.get("dt_txt", "")[:10]
        if dt_txt == target_date:
            t_max = entry.get("main", {}).get("temp_max")
            if t_max is not None:
                temps.append(float(t_max))
    
    return max(temps) if temps else None

def get_precip_mm(city: str, target_date: str) -> Optional[float]:
    """Retourne la précipitation totale (mm) prévue pour une date."""
    data = get_forecast(city)
    if not data:
        return None
    
    total = 0.0
    for entry in data.get("list", []):
        dt_txt = entry.get("dt_txt", "")[:10]
        if dt_txt == target_date:
            rain = entry.get("rain", {}).get("3h", 0) or 0
            snow = entry.get("snow", {}).get("3h", 0) or 0
            total += float(rain) + float(snow)
    
    return total if total > 0 else 0.0

if __name__ == "__main__":
    # Test rapide
    city = "Buenos Aires"
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"Test OWM — {city} — {date}")
    temp = get_max_temp_for_date(city, date)
    precip = get_precip_mm(city, date)
    print(f"  Temp max prévue : {temp}°C")
    print(f"  Précip prévue   : {precip}mm")