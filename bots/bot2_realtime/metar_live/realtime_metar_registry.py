"""
Oracle METAR « sniper » : interrogation NOAA / Aviation Weather Center (sans clé API).

API : https://aviationweather.gov/api/data/metar
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class RealTimeMetarRegistry:
    def __init__(self, timeout: float = 5.0):
        # API Aviation Weather (NOAA) - Pas de clé nécessaire
        self.base_url = "https://aviationweather.gov/api/data/metar"
        self.timeout = timeout

    def fetch_latest_metar(self, icao_code: str) -> Optional[dict[str, Any]]:
        """
        Interroge l'API NOAA pour récupérer le dernier rapport METAR de la station.
        """
        params = {
            "ids": icao_code.upper(),
            "format": "json",
            "taf": "false",
        }

        try:
            logger.info("[METAR] Requête pour la station %s...", icao_code)
            # Un timeout court (ex: 5s) est crucial en scalping
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(self.base_url, params=params)
                response.raise_for_status()
                data = response.json()

            if not data or len(data) == 0:
                logger.warning("[METAR] Aucune donnée renvoyée pour %s.", icao_code)
                return None

            # L'API renvoie le plus récent en premier
            latest = data[0]
            temp_c = latest.get("temp")

            if temp_c is None:
                logger.warning("[METAR] Relevé reçu pour %s mais température manquante.", icao_code)
                return None

            # Conversion robuste C -> F
            temp_f = (temp_c * 9 / 5) + 32.0

            result = {
                "icao": icao_code,
                "temp_c": round(temp_c, 1),
                "temp_f": round(temp_f, 1),
                "obsTime": latest.get("receiptTime"),  # Heure officielle du relevé
                "rawOb": latest.get("rawOb"),  # Le message brut OMM
            }

            logger.info(
                "[METAR] Succès %s : %s°F (%s°C)",
                icao_code,
                result["temp_f"],
                result["temp_c"],
            )
            return result

        except httpx.ReadTimeout:
            logger.error("[METAR] Timeout de l'API NOAA pour %s (> %ss).", icao_code, self.timeout)
            return None
        except httpx.RequestError as e:
            logger.error("[METAR] Erreur réseau pour %s : %s", icao_code, e)
            return None
        except Exception as e:
            logger.error("[METAR] Erreur inattendue pour %s : %s", icao_code, e)
            return None
