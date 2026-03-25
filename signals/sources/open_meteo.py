"""
Open-Meteo — API météo gratuite, sans clé, haute précision.
https://open-meteo.com/

Modèles utilisés : Best Match (agrège automatiquement les meilleurs modèles locaux)

Gestion des dates :
  - Date future  → forecast API (api.open-meteo.com)
  - Date passée  → archive API (archive-api.open-meteo.com)
  - Date du jour → forecast d'abord, archive en fallback

IMPORTANT : avec timezone=auto, Open-Meteo utilise l'heure locale des coordonnées.
Pour une ville comme Singapore (UTC+8), à 23h UTC le "today" local peut être
différent de "today" UTC, rendant la date du marché "passée" en heure locale.
Le fallback archive corrige ce comportement.
"""
from __future__ import annotations

import json, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional

GEOCODE_URL  = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"

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


def _fetch_daily_temp_max(
    api_url: str,
    lat: float,
    lon: float,
    date_str: str,
    city: str,
    max_retries: int = 2,
    model: Optional[str] = None,
) -> Optional[float]:
    """
    Appel générique forecast ou archive pour temperature_2m_max.
    Réessaie jusqu à max_retries fois en cas d échec réseau (timeout, 5xx).

    ``model`` : id Open-Meteo (ex. ``ecmwf_ifs04``, ``gfs_hrrr``) — uniquement pour
    l'endpoint **forecast** ; l'archive est appelée sans modèle.
    """
    import time as _time
    url = (
        f"{api_url}"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max"
        f"&timezone=auto"
        f"&start_date={date_str}&end_date={date_str}"
    )
    if model and "archive" not in api_url.lower():
        url += f"&models={urllib.parse.quote(model)}"
    for attempt in range(1, max_retries + 1):
        try:
            data  = _get(url)
            temps = data.get("daily", {}).get("temperature_2m_max", [])
            if temps and temps[0] is not None:
                return float(temps[0])
            return None   # API a répondu mais pas de données pour cette date
        except Exception as e:
            if attempt < max_retries:
                _time.sleep(1)   # courte pause avant retry
            else:
                host = api_url.split("/")[2][:20] if "/" in api_url else api_url[:20]
                print(f"  ⚠️ Open-Meteo [{host}] [{city}] [{date_str}] (après {max_retries} tentatives): {e}")
    return None


def get_max_temp(city: str, date_str: str, model: Optional[str] = None) -> Optional[float]:
    """
    Température max prévue ou observée (°C) pour une ville à une date YYYY-MM-DD.

    Stratégie : forecast d'abord, archive en fallback systématique.
    Pas de logique de date côté client : l'API sait quelles données elle a.

    ``model`` : modèle Open-Meteo forcé sur le **forecast** (ex. ``ecmwf_ifs04``,
    ``gfs_hrrr``). None = comportement API par défaut (best match).

    Raison : avec timezone=auto, Open-Meteo interprète les dates en heure locale.
    Pour une ville UTC+8 comme Singapore, la date J peut déjà être «aujourd'hui»
    en heure locale même si UTC est encore J-1. Le forecast peut retourner []
    dans ce cas — l'archive corrige cela en fournissant les données observées.
    """
    coords = geocode(city)
    if coords is None:
        return None
    lat, lon = coords

    # 1. Forecast (couvre jusqu'à J+16)
    result = _fetch_daily_temp_max(FORECAST_URL, lat, lon, date_str, city, model=model)
    if result is not None:
        return result

    # 2. Archive (couvre J-2 et avant, délai de publication ~2-5 jours selon station)
    result = _fetch_daily_temp_max(ARCHIVE_URL, lat, lon, date_str, city, model=None)
    return result



def get_min_temp(city: str, date_str: str) -> Optional[float]:
    """Température min prévue ou observée (°C) — même stratégie que get_max_temp."""
    coords = geocode(city)
    if coords is None:
        return None
    lat, lon = coords

    def _fetch(api_url: str) -> Optional[float]:
        url = (
            f"{api_url}"
            f"?latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_min"
            f"&timezone=auto"
            f"&start_date={date_str}&end_date={date_str}"
        )
        try:
            data = _get(url)
            temps = data.get("daily", {}).get("temperature_2m_min", [])
            return float(temps[0]) if temps and temps[0] is not None else None
        except Exception as e:
            host = api_url.split("/")[2][:20]
            print(f"  ⚠️ Open-Meteo [{host}] min_temp [{city}]: {e}")
            return None

    result = _fetch(FORECAST_URL)
    if result is not None:
        return result
    return _fetch(ARCHIVE_URL)


def get_precip_mm(city: str, date_str: str) -> Optional[float]:
    """Précipitations totales (mm) prévues."""
    coords = geocode(city)
    if coords is None:
        return None
    lat, lon = coords
    try:
        today_utc   = datetime.now(timezone.utc).date()
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        api_url     = FORECAST_URL if target_date >= today_utc else ARCHIVE_URL

        var = "precipitation_sum" if api_url == FORECAST_URL else "precipitation_sum"
        url = (
            f"{api_url}"
            f"?latitude={lat}&longitude={lon}"
            f"&daily={var}"
            f"&timezone=auto"
            f"&start_date={date_str}&end_date={date_str}"
        )
        data = _get(url)
        vals = data.get("daily", {}).get(var, [])
        if vals and vals[0] is not None:
            return float(vals[0])
        # Fallback si l'autre endpoint a la donnée
        other_url = ARCHIVE_URL if api_url == FORECAST_URL else FORECAST_URL
        url2 = (
            f"{other_url}"
            f"?latitude={lat}&longitude={lon}"
            f"&daily={var}"
            f"&timezone=auto"
            f"&start_date={date_str}&end_date={date_str}"
        )
        data2 = _get(url2)
        vals2 = data2.get("daily", {}).get(var, [])
        return float(vals2[0]) if vals2 and vals2[0] is not None else None
    except Exception as e:
        print(f"  ⚠️ Open-Meteo precip [{city}]: {e}")
        return None


def get_wind_max(city: str, date_str: str) -> Optional[float]:
    """Vitesse max du vent (km/h) prévue ou observée — même stratégie que get_max_temp."""
    coords = geocode(city)
    if coords is None:
        return None
    lat, lon = coords

    def _fetch(api_url: str) -> Optional[float]:
        url = (
            f"{api_url}"
            f"?latitude={lat}&longitude={lon}"
            f"&daily=wind_speed_10m_max"
            f"&timezone=auto"
            f"&start_date={date_str}&end_date={date_str}"
        )
        try:
            data = _get(url)
            vals = data.get("daily", {}).get("wind_speed_10m_max", [])
            return float(vals[0]) if vals and vals[0] is not None else None
        except Exception as e:
            host = api_url.split("/")[2][:20]
            print(f"  ⚠️ Open-Meteo [{host}] wind [{city}]: {e}")
            return None

    result = _fetch(FORECAST_URL)
    if result is not None:
        return result
    return _fetch(ARCHIVE_URL)


if __name__ == "__main__":
    from datetime import timedelta
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    today     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow  = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    tests = [
        ("Singapore", yesterday, "passé"),
        ("Singapore", today,     "aujourd'hui"),
        ("Singapore", tomorrow,  "demain"),
        ("London",    today,     "aujourd'hui"),
        ("Buenos Aires", yesterday, "passé"),
    ]
    print("🌤️  Test Open-Meteo — forecast + archive fallback\n")
    for city, date, label in tests:
        tmax = get_max_temp(city, date)
        print(f"  {city:<20} {date}  ({label:<14})  max={tmax}°C")