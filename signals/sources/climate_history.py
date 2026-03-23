"""
Climatologie historique via Open-Meteo Archive API.
Gratuit, sans clé, données depuis 1940.
https://open-meteo.com/en/docs/historical-weather-api

Fournit :
- Distribution historique des températures max/min par ville/mois
- Percentiles (P10, P25, P50, P75, P90)
- Probabilité climatologique P(temp >= X) basée sur 30 ans
- Tendance des 7 derniers jours
"""
from __future__ import annotations
import json, urllib.request, urllib.parse, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import math
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple
from pathlib import Path

ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"
GEOCODE_URL  = "https://geocoding-api.open-meteo.com/v1/search"

# Cache local pour éviter de re-télécharger les données historiques
CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "raw" / "climate_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Coordonnées hardcodées — évite le géocodage réseau pour les villes connues
_CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york": (40.71, -74.01), "nyc": (40.71, -74.01),
    "los angeles": (34.05, -118.24), "chicago": (41.85, -87.65),
    "miami": (25.77, -80.19), "houston": (29.76, -95.37),
    "phoenix": (33.45, -112.07), "atlanta": (33.75, -84.39),
    "dallas": (32.78, -96.80), "seattle": (47.61, -122.33),
    "denver": (39.74, -104.98), "boston": (42.36, -71.06),
    "san francisco": (37.77, -122.42), "las vegas": (36.17, -115.14),
    "minneapolis": (44.98, -93.27), "toronto": (43.65, -79.38),
    "london": (51.51, -0.13), "paris": (48.85, 2.35),
    "berlin": (52.52, 13.40), "madrid": (40.42, -3.70),
    "rome": (41.90, 12.50), "milan": (45.46, 9.19),
    "amsterdam": (52.37, 4.90), "brussels": (50.85, 4.35),
    "vienna": (48.21, 16.37), "zurich": (47.38, 8.54),
    "munich": (48.14, 11.58), "stockholm": (59.33, 18.07),
    "oslo": (59.91, 10.75), "warsaw": (52.23, 21.01),
    "prague": (50.08, 14.44), "budapest": (47.50, 19.04),
    "lisbon": (38.72, -9.14), "athens": (37.98, 23.73),
    "istanbul": (41.01, 28.95), "ankara": (39.93, 32.86),
    "dublin": (53.33, -6.25), "copenhagen": (55.68, 12.57),
    "helsinki": (60.17, 24.94), "bucharest": (44.43, 26.10),
    "tokyo": (35.69, 139.69), "beijing": (39.91, 116.39),
    "shanghai": (31.23, 121.47), "shenzhen": (22.54, 114.06),
    "wuhan": (30.58, 114.27), "chongqing": (29.56, 106.55),
    "xi an": (34.27, 108.95), "nanjing": (32.06, 118.78),
    "tianjin": (39.13, 117.18), "chengdu": (30.66, 104.07),
    "guangzhou": (23.13, 113.26), "seoul": (37.57, 126.98),
    "singapore": (1.29, 103.85), "mumbai": (19.08, 72.88),
    "delhi": (28.61, 77.21), "new delhi": (28.61, 77.21),
    "lucknow": (26.85, 80.95), "bangkok": (13.75, 100.52),
    "jakarta": (-6.21, 106.85), "dubai": (25.20, 55.27),
    "karachi": (24.86, 67.01), "hong kong": (22.32, 114.17),
    "taipei": (25.05, 121.53), "osaka": (34.69, 135.50),
    "kuala lumpur": (3.14, 101.69), "manila": (14.60, 120.98),
    "sydney": (-33.87, 151.21), "melbourne": (-37.81, 144.96),
    "buenos aires": (-34.60, -58.38), "sao paulo": (-23.55, -46.63),
    "rio de janeiro": (-22.91, -43.17), "mexico city": (19.43, -99.13),
    "cairo": (30.05, 31.25), "lagos": (6.46, 3.38),
    "nairobi": (-1.29, 36.82), "johannesburg": (-26.20, 28.04),
    "cape town": (-33.93, 18.42), "santiago": (-33.46, -70.65),
    "lima": (-12.04, -77.03), "bogota": (4.71, -74.07),
}

# Nombre d'années d'historique à utiliser
HISTORY_YEARS = 15  # 15 ans = bon équilibre stabilité/modernité


# ── Géocodage ─────────────────────────────────────────────────────────────────

_geo_cache: Dict[str, Optional[Tuple[float, float]]] = {}

def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def geocode(city: str) -> Optional[Tuple[float, float]]:
    if city in _geo_cache:
        return _geo_cache[city]
    try:
        url = f"{GEOCODE_URL}?name={urllib.parse.quote(city)}&count=1&language=en"
        data = _get(url)
        results = data.get("results")
        if not results:
            _geo_cache[city] = None
            return None
        coords = (results[0]["latitude"], results[0]["longitude"])
        _geo_cache[city] = coords
        return coords
    except Exception as e:
        print(f"  ⚠️ Geocode [{city}]: {e}")
        _geo_cache[city] = None
        return None


# ── Cache local ───────────────────────────────────────────────────────────────

def _cache_path(city: str, month: int) -> Path:
    safe = city.lower().replace(" ", "_")
    return CACHE_DIR / f"{safe}_month{month:02d}.json"

def _load_cache(city: str, month: int) -> Optional[List[float]]:
    path = _cache_path(city, month)
    if not path.exists():
        return None
    # Cache valide 7 jours
    age = datetime.now().timestamp() - path.stat().st_mtime
    if age > 7 * 86400:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

def _save_cache(city: str, month: int, data: List[float]) -> None:
    path = _cache_path(city, month)
    with open(path, "w") as f:
        json.dump(data, f)


# ── Récupération historique ───────────────────────────────────────────────────

def get_historical_max_temps(city: str, month: int) -> Optional[List[float]]:
    """
    Retourne la liste des températures max historiques pour un mois donné.
    Ex: toutes les temp max de mars sur 15 ans à Madrid = ~465 valeurs.
    Mise en cache 7 jours.
    """
    cached = _load_cache(city, month)
    if cached is not None:
        return cached

    coords = geocode(city)
    if coords is None:
        return None

    lat, lon = coords
    now = datetime.now(timezone.utc)
    end_year   = now.year - 1  # Jusqu'à l'année dernière (complète)
    start_year = end_year - HISTORY_YEARS + 1

    all_temps: List[float] = []

    for year in range(start_year, end_year + 1):
        # Dernier jour du mois
        if month == 12:
            last_day = 31
        else:
            last_day = (datetime(year, month + 1, 1) - timedelta(days=1)).day

        start_date = f"{year}-{month:02d}-01"
        end_date   = f"{year}-{month:02d}-{last_day:02d}"

        try:
            url = (
                f"{ARCHIVE_URL}"
                f"?latitude={lat}&longitude={lon}"
                f"&start_date={start_date}&end_date={end_date}"
                f"&daily=temperature_2m_max"
                f"&timezone=auto"
            )
            data = _get(url)
            temps = data.get("daily", {}).get("temperature_2m_max", [])
            all_temps.extend([t for t in temps if t is not None])
        except Exception as e:
            print(f"  ⚠️ Archive [{city} {year}/{month:02d}]: {e}")
            continue

    if not all_temps:
        return None

    _save_cache(city, month, all_temps)
    return all_temps


def get_recent_temps(city: str, days: int = 7) -> Optional[List[float]]:
    """
    Retourne les températures max des N derniers jours (tendance récente).
    """
    coords = geocode(city)
    if coords is None:
        return None

    lat, lon = coords
    now        = datetime.now(timezone.utc)
    end_date   = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    start_date = (now - timedelta(days=days)).strftime("%Y-%m-%d")

    try:
        url = (
            f"{ARCHIVE_URL}"
            f"?latitude={lat}&longitude={lon}"
            f"&start_date={start_date}&end_date={end_date}"
            f"&daily=temperature_2m_max"
            f"&timezone=auto"
        )
        data = _get(url)
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        return [t for t in temps if t is not None]
    except Exception as e:
        print(f"  ⚠️ Recent temps [{city}]: {e}")
        return None


# ── Statistiques climatologiques ──────────────────────────────────────────────

def _percentile(data: List[float], p: float) -> float:
    """Calcule le percentile p (0-100) d'une liste."""
    sorted_data = sorted(data)
    n = len(sorted_data)
    idx = (p / 100) * (n - 1)
    lo  = int(idx)
    hi  = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_data[lo] + frac * (sorted_data[hi] - sorted_data[lo])

def _mean(data: List[float]) -> float:
    return sum(data) / len(data)

def _std(data: List[float]) -> float:
    m = _mean(data)
    return math.sqrt(sum((x - m) ** 2 for x in data) / len(data))


def get_climate_stats(city: str, month: int) -> Optional[Dict]:
    """
    Retourne les statistiques climatologiques complètes pour une ville/mois.

    Retourne:
        mean     : température moyenne historique
        std      : écart-type historique
        p10/p25/p50/p75/p90 : percentiles
        n        : nombre d'observations
    """
    temps = get_historical_max_temps(city, month)
    if not temps or len(temps) < 10:
        return None

    return {
        "mean": round(_mean(temps), 2),
        "std":  round(_std(temps), 2),
        "p10":  round(_percentile(temps, 10), 2),
        "p25":  round(_percentile(temps, 25), 2),
        "p50":  round(_percentile(temps, 50), 2),
        "p75":  round(_percentile(temps, 75), 2),
        "p90":  round(_percentile(temps, 90), 2),
        "min":  round(min(temps), 2),
        "max":  round(max(temps), 2),
        "n":    len(temps),
    }


def climate_prob_above(city: str, month: int, threshold: float) -> Optional[float]:
    """
    P(temp_max >= threshold) basée sur l'historique climatologique.
    Ex: P(temp_max >= 21°C) à Madrid en mars = ?
    """
    temps = get_historical_max_temps(city, month)
    if not temps:
        return None
    above = sum(1 for t in temps if t >= threshold)
    return round(above / len(temps), 4)


def climate_prob_exact(city: str, month: int, target: float,
                       tolerance: float = 0.5) -> Optional[float]:
    """
    P(temp_max ∈ [target - tolerance, target + tolerance])
    basée sur l'historique climatologique.
    """
    temps = get_historical_max_temps(city, month)
    if not temps:
        return None
    in_range = sum(1 for t in temps if abs(t - target) <= tolerance)
    return round(in_range / len(temps), 4)


def get_recent_trend(city: str, days: int = 7) -> Optional[Dict]:
    """
    Analyse la tendance des N derniers jours.
    Retourne la moyenne récente et la pente de tendance.
    """
    temps = get_recent_temps(city, days)
    if not temps or len(temps) < 2:
        return None

    mean_recent = _mean(temps)

    # Régression linéaire simple pour la pente
    n    = len(temps)
    xs   = list(range(n))
    xm   = _mean(xs)
    ym   = mean_recent
    num  = sum((xs[i] - xm) * (temps[i] - ym) for i in range(n))
    den  = sum((xs[i] - xm) ** 2 for i in range(n))
    slope = num / den if den != 0 else 0.0

    return {
        "temps":       temps,
        "mean_recent": round(mean_recent, 2),
        "slope_per_day": round(slope, 3),  # °C/jour (positif = réchauffement)
        "last_temp":   temps[-1],
        "n_days":      n,
    }


if __name__ == "__main__":
    cities = ["Madrid", "Munich", "Chicago", "Seoul", "Buenos Aires"]
    month  = datetime.now().month

    print(f"📊 Climatologie historique — mois {month} ({HISTORY_YEARS} ans)\n")
    print(f"{'Ville':<20} {'Moy':>6} {'Std':>5} {'P10':>6} {'P50':>6} {'P90':>6} {'N':>5}")
    print("─" * 60)

    for city in cities:
        stats = get_climate_stats(city, month)
        if stats:
            print(
                f"{city:<20} {stats['mean']:>6.1f} {stats['std']:>5.1f} "
                f"{stats['p10']:>6.1f} {stats['p50']:>6.1f} {stats['p90']:>6.1f} "
                f"{stats['n']:>5}"
            )
        else:
            print(f"{city:<20} N/A")

    print(f"\n📈 Tendance récente (7 jours)\n")
    for city in cities:
        trend = get_recent_trend(city, 7)
        if trend:
            sign = "+" if trend["slope_per_day"] >= 0 else ""
            print(
                f"  {city:<20} moy={trend['mean_recent']:.1f}°C  "
                f"tendance={sign}{trend['slope_per_day']:.2f}°C/j  "
                f"hier={trend['last_temp']:.1f}°C"
            )

    print(f"\n🎯 Test probabilités climatologiques — Madrid mars\n")
    for threshold in [15, 17, 19, 21, 23]:
        p_above = climate_prob_above("Madrid", month, threshold)
        p_exact = climate_prob_exact("Madrid", month, threshold)
        print(
            f"  P(max >= {threshold}°C) = {f'{p_above*100:.1f}%' if p_above else 'N/A':>6}  "
            f"P(max ≈ {threshold}°C) = {f'{p_exact*100:.1f}%' if p_exact else 'N/A':>6}"
        )