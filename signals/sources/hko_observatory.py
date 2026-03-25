"""
Hong Kong Observatory — prévision 9 jours (dataType=fnd), API ouverte.

https://www.hko.gov.hk/en/abouthko/opendata_intro.htm

Utiliser uniquement pour les marchés liés à Hong Kong ; respecter les conditions
d’usage du site HKO.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Optional

HKO_FND_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=fnd&lang=en"


def _is_hk_city(city: str) -> bool:
    c = (city or "").strip().lower()
    return "hong kong" in c or c in ("hk", "hksar", "香港")


def _get_json(url: str) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; POLYMARKET-bot/1.0)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def get_max_temp(city: str, date_str: str) -> Optional[float]:
    """
    Température max (°C) pour la date cible depuis la prévision officielle HKO.

    Retourne None si la ville n’est pas HK ou si la date est absente du flux.
    """
    if not _is_hk_city(city):
        return None
    target = (date_str or "").replace("-", "")
    if len(target) != 8:
        return None

    data = _get_json(HKO_FND_URL)
    if not data:
        return None

    days = data.get("weatherForecast")
    if not isinstance(days, list):
        return None

    for row in days:
        if not isinstance(row, dict):
            continue
        fd = str(row.get("forecastDate") or "")
        if fd != target:
            continue
        mx = row.get("forecastMaxtemp") or row.get("forecastMaxTemp")
        if isinstance(mx, dict):
            v = mx.get("value")
            if v is not None:
                return float(v)
        return None

    return None
