"""
Oracle METAR « sniper » : interrogation NOAA / Aviation Weather Center (sans clé API).

API : https://aviationweather.gov/api/data/metar

Retries / timeout configurables via variables d’environnement :
``METAR_TIMEOUT_SEC``, ``METAR_MAX_RETRIES``, ``METAR_RETRY_BACKOFF_SEC``.
"""
from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class RealTimeMetarRegistry:
    def __init__(
        self,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        retry_backoff_sec: Optional[float] = None,
    ):
        self.base_url = "https://aviationweather.gov/api/data/metar"
        self.timeout = float(timeout) if timeout is not None else _env_float("METAR_TIMEOUT_SEC", 5.0)
        self.max_retries = max_retries if max_retries is not None else _env_int("METAR_MAX_RETRIES", 2)
        self.retry_backoff_sec = (
            retry_backoff_sec if retry_backoff_sec is not None else _env_float("METAR_RETRY_BACKOFF_SEC", 0.5)
        )

    def fetch_latest_metar(self, icao_code: str) -> Optional[dict[str, Any]]:
        """
        Interroge l'API NOAA pour récupérer le dernier rapport METAR de la station.
        Retries avec backoff exponentiel sur timeout, erreurs réseau et HTTP 5xx.
        """
        params = {
            "ids": icao_code.upper(),
            "format": "json",
            "taf": "false",
        }

        last_err: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            try:
                logger.info("[METAR] Requête pour la station %s (essai %s/%s)...", icao_code, attempt + 1, self.max_retries + 1)
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.get(self.base_url, params=params)
                    response.raise_for_status()
                    data = response.json()

                if not data or len(data) == 0:
                    logger.warning("[METAR] Aucune donnée renvoyée pour %s.", icao_code)
                    return None

                latest = data[0]
                temp_c = latest.get("temp")

                if temp_c is None:
                    logger.warning("[METAR] Relevé reçu pour %s mais température manquante.", icao_code)
                    return None

                temp_f = (temp_c * 9 / 5) + 32.0

                result = {
                    "icao": icao_code,
                    "temp_c": round(temp_c, 1),
                    "temp_f": round(temp_f, 1),
                    "obsTime": latest.get("receiptTime"),
                    "rawOb": latest.get("rawOb"),
                }

                logger.info(
                    "[METAR] Succès %s : %s°F (%s°C)",
                    icao_code,
                    result["temp_f"],
                    result["temp_c"],
                )
                return result

            except httpx.TimeoutException as e:
                last_err = e
                logger.warning("[METAR] Timeout API pour %s (> %ss) — essai %s.", icao_code, self.timeout, attempt + 1)
            except httpx.HTTPStatusError as e:
                last_err = e
                code = e.response.status_code if e.response is not None else 0
                if code >= 500 and attempt < self.max_retries:
                    logger.warning("[METAR] HTTP %s pour %s — retry.", code, icao_code)
                else:
                    logger.error("[METAR] HTTP %s pour %s : %s", code, icao_code, e)
                    return None
            except httpx.RequestError as e:
                last_err = e
                logger.warning("[METAR] Erreur réseau pour %s : %s", icao_code, e)
            except Exception as e:
                last_err = e
                logger.error("[METAR] Erreur inattendue pour %s : %s", icao_code, e)
                return None

            if attempt < self.max_retries:
                sleep_s = self.retry_backoff_sec * (2**attempt) + random.uniform(0, 0.25)
                time.sleep(sleep_s)

        if last_err is not None:
            logger.error("[METAR] Abandon après %s échecs pour %s : %s", self.max_retries + 1, icao_code, last_err)
        return None
