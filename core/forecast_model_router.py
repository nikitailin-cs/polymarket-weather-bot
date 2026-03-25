"""
Routage des prévisions par **contexte géographique et horizon**, au lieu d'une moyenne
multi-sources indifférenciée.

Règles par défaut (surcharge via variables d'environnement) :

- **Hong Kong** : prévision officielle **HKO** (API ouverte data.weather.gov.hk), pas une moyenne.
- **États-Unis (CONUS), court terme** : modèle Open-Meteo incluant **HRRR** via ``gfs_hrrr``
  (identifiant API stable ; ``hrrr_conus`` seul peut renvoyer 400 sur l'endpoint forecast générique).
- **Moyen terme (global)** : **ECMWF** via ``ecmwf_ifs04`` sur l'API forecast standard.
- **Court terme hors US / HK** : ``ecmwf_ifs04`` ou ``best_match`` (None) selon env.

Voir aussi : ``signals/sources/aggregator.get_max_temp_model_routed``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class ForecastRouting:
    """Décision de routage pour une requête température max."""

    primary: str  # "hko" | "open_meteo"
    open_meteo_model: Optional[str]
    """Modèle Open-Meteo (ex. ecmwf_ifs04, gfs_hrrr). None = défaut API (best_match)."""

    reason: str


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _days_to_target(date_str: str) -> int:
    try:
        y, m, d = int(date_str[0:4]), int(date_str[5:7]), int(date_str[8:10])
        tgt = date(y, m, d)
    except (TypeError, ValueError, IndexError):
        return 0
    today = datetime.now(timezone.utc).date()
    return max(0, (tgt - today).days)


def _is_hong_kong(city: str, lat: Optional[float], lon: Optional[float]) -> bool:
    c = (city or "").lower()
    if "hong kong" in c or c.strip() in ("hk", "hksar"):
        return True
    if lat is not None and lon is not None:
        # Boîte approximative territoire HK
        return 22.05 <= lat <= 22.65 and 113.75 <= lon <= 114.55
    return False


def _is_us_conus(lat: float, lon: float) -> bool:
    return 24.0 <= lat <= 50.0 and -125.0 <= lon <= -66.0


def route_max_temp_forecast(
    city: str,
    date_str: str,
    *,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
) -> ForecastRouting:
    """
    Choisit la source / modèle pour la température max du jour cible.

    ``lat``/``lon`` optionnels : si absents, géocodage via ``open_meteo.geocode(city)``.
    """
    if lat is None or lon is None:
        from signals.sources import open_meteo as om

        coords = om.geocode(city)
        if coords is None:
            return ForecastRouting(
                primary="open_meteo",
                open_meteo_model=None,
                reason="geocode_failed_open_meteo_default",
            )
        lat, lon = coords

    days = _days_to_target(date_str)

    us_short_days = _env_int("FORECAST_US_SHORT_TERM_DAYS", 2)
    medium_days = _env_int("FORECAST_MEDIUM_TERM_MIN_DAYS", 3)

    model_medium = (os.getenv("FORECAST_OPEN_METEO_MEDIUM_MODEL") or "ecmwf_ifs04").strip()
    model_us_short = (os.getenv("FORECAST_OPEN_METEO_US_SHORT_MODEL") or "gfs_hrrr").strip()
    model_short_row = (os.getenv("FORECAST_OPEN_METEO_SHORT_REST_MODEL") or model_medium).strip()

    if _is_hong_kong(city, lat, lon):
        return ForecastRouting(primary="hko", open_meteo_model=None, reason="hong_kong_hko")

    if _is_us_conus(lat, lon):
        if days <= us_short_days:
            return ForecastRouting(
                primary="open_meteo",
                open_meteo_model=model_us_short or None,
                reason="us_conus_short_hrrr_blend",
            )
        if days >= medium_days:
            return ForecastRouting(
                primary="open_meteo",
                open_meteo_model=model_medium or None,
                reason="us_conus_medium_ecmwf",
            )
        return ForecastRouting(
            primary="open_meteo",
            open_meteo_model=model_us_short or None,
            reason="us_conus_mid_horizon_hrrr_blend",
        )

    if days >= medium_days:
        return ForecastRouting(
            primary="open_meteo",
            open_meteo_model=model_medium or None,
            reason="global_medium_ecmwf",
        )

    return ForecastRouting(
        primary="open_meteo",
        open_meteo_model=model_short_row or None,
        reason="global_short",
    )


def forecast_model_routing_enabled() -> bool:
    return (os.getenv("FORECAST_MODEL_ROUTING") or "").strip().lower() in ("1", "true", "yes", "on")
