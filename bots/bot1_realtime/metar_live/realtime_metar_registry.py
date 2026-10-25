"""
Registre METAR temps réel via l’API publique NOAA / Aviation Weather Center.

- Client **HTTPX** (timeouts agressifs, retries bornés).
- Priorité **SPECI** > METAR horaire lorsque le même horodatage de tri est atteint.
- Conversions **°C → °F** alignées sur ``ContractSpec.evaluate`` / UMA Polymarket
  (``round(valeur, oracle_rounding)``, défaut entier).
- Horodatage **UTC → heure locale** aéroport (IANA) pour traçabilité et deadline.

Documentation API : https://aviationweather.gov/data/api/
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx

from core.metar_observation import MetarObservation, is_speci_raw  # noqa: F401

LOG = logging.getLogger(__name__)

AWC_METAR_BASE = os.getenv(
    "BOT1_METAR_API_URL", "https://aviationweather.gov/api/data/metar"
).rstrip("/")


def celsius_to_fahrenheit_uma(temp_c: float, oracle_rounding: int = 0) -> float:
    """
    °C → °F puis arrondi comme l’oracle UMA (identique à ``round(x, oracle_rounding)``
    dans ``ContractSpec.evaluate`` pour une observation en °F).
    """
    f_raw = temp_c * 9.0 / 5.0 + 32.0
    return round(f_raw, oracle_rounding)


def celsius_to_celsius_uma(temp_c: float, oracle_rounding: int = 0) -> float:
    """Observation en °C arrondie comme l’oracle (souvent entier, ``oracle_rounding=0``)."""
    return round(temp_c, oracle_rounding)


def utc_observation_to_local(dt_utc: datetime, tz_name: str) -> datetime:
    """``dt_utc`` (aware) → même instant en fuseau IANA de l’aéroport."""
    name = (tz_name or "UTC").strip() or "UTC"
    try:
        return dt_utc.astimezone(ZoneInfo(name))
    except Exception:
        LOG.warning("timezone invalide %r — fallback UTC", tz_name)
        return dt_utc.astimezone(timezone.utc)


def _oracle_rounding_from_env() -> int:
    try:
        return int(os.getenv("BOT1_RT_ORACLE_ROUNDING", "0"))
    except ValueError:
        return 0


def temp_celsius_to_contract_unit(
    temp_c: float,
    unit: str,
    *,
    oracle_rounding: int | None = None,
) -> float:
    """
    Température METAR (°C API) → unité du marché, avec arrondi oracle Polymarket.
    ``unit`` : F / FAHRENHEIT / C / CELSIUS.
    """
    r = _oracle_rounding_from_env() if oracle_rounding is None else oracle_rounding
    u = (unit or "F").strip().upper()
    if u in ("C", "CELSIUS"):
        return celsius_to_celsius_uma(temp_c, r)
    if u in ("F", "FAHRENHEIT"):
        return celsius_to_fahrenheit_uma(temp_c, r)
    raise ValueError(f"threshold_unit inconnu: {unit!r} (utiliser F ou C)")




def _parse_report_time(item: dict[str, Any]) -> tuple[float, str]:
    """Retourne (clé de tri unix, chaîne affichable)."""
    obs = item.get("obsTime")
    if isinstance(obs, (int, float)):
        t = float(obs)
        rt = item.get("reportTime")
        label = rt.strip() if isinstance(rt, str) and rt.strip() else str(int(t))
        return (t, label)
    rt = item.get("reportTime")
    if isinstance(rt, str) and rt.strip():
        s = rt.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (dt.timestamp(), rt.strip())
        except ValueError:
            return (0.0, rt.strip())
    return (0.0, "")


def observation_utc_from_item(item: dict[str, Any]) -> datetime | None:
    """Construit un datetime UTC aware à partir des champs AWC."""
    obs = item.get("obsTime")
    if isinstance(obs, (int, float)):
        return datetime.fromtimestamp(float(obs), tz=timezone.utc)
    rt = item.get("reportTime")
    if isinstance(rt, str) and rt.strip():
        s = rt.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _row_priority_key(item: dict[str, Any]) -> tuple[float, int]:
    """
    Clé de tri décroissante : (instant obs, bonus SPECI).
    SPECI l’emporte sur METAR si même fenêtre de tri temporelle.
    """
    sort_key, _ = _parse_report_time(item)
    raw = str(item.get("rawOb", "") or "")
    speci = 1 if is_speci_raw(raw) else 0
    return (sort_key, speci)




def _pick_best_per_station(rows: list[dict[str, Any]]) -> dict[str, MetarObservation]:
    """Garde la meilleure observation par ``icaoId`` (temps puis SPECI > METAR)."""
    best: dict[str, tuple[tuple[float, int], MetarObservation]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        icao = str(item.get("icaoId", "")).strip().upper()
        if not icao:
            continue
        temp = item.get("temp")
        if temp is None:
            continue
        try:
            temp_c = float(temp)
        except (TypeError, ValueError):
            continue
        pkey = _row_priority_key(item)
        raw = str(item.get("rawOb", "") or "")
        rt_display = str(item.get("reportTime", "") or "").strip() or str(item.get("obsTime", ""))
        obs_utc = observation_utc_from_item(item)
        if obs_utc is None:
            obs_utc = datetime.now(timezone.utc)
            LOG.debug("obsTime/reportTime manquant pour %s — now UTC de secours", icao)
        obs = MetarObservation(
            icao_id=icao,
            temp_celsius=temp_c,
            report_time_utc=rt_display,
            raw_text=raw,
            observation_utc=obs_utc,
            is_speci=is_speci_raw(raw),
        )
        prev = best.get(icao)
        if prev is None or pkey > prev[0]:
            best[icao] = (pkey, obs)
    return {k: v[1] for k, v in best.items()}


class RealTimeMetarRegistry:
    """
    Client HTTPX pour les derniers METAR par station OACI.

    Paramètres GET usuels AWC : ``ids``, ``format=json``, ``hours``.
    """

    def __init__(
        self,
        *,
        timeout_sec: float | None = None,
        hours: int | None = None,
        max_retries: int | None = None,
        retry_backoff_sec: float | None = None,
    ) -> None:
        self.timeout_sec = float(
            timeout_sec if timeout_sec is not None else os.getenv("BOT1_METAR_HTTP_TIMEOUT_SEC", "3")
        )
        self.hours = int(hours if hours is not None else os.getenv("BOT1_METAR_HOURS", "2"))
        self.max_retries = max(
            1,
            int(max_retries if max_retries is not None else os.getenv("BOT1_METAR_HTTP_RETRIES", "3")),
        )
        self.retry_backoff_sec = float(
            retry_backoff_sec
            if retry_backoff_sec is not None
            else os.getenv("BOT1_METAR_RETRY_BACKOFF_SEC", "0.12")
        )

    def fetch_latest_for_stations(self, icao_list: Iterable[str]) -> dict[str, MetarObservation]:
        """
        Récupère les METAR les plus récents pour chaque OACI demandé.

        Retourne ``ICAO -> MetarObservation`` (stations sans température omises).
        """
        ids = sorted({str(x).strip().upper() for x in icao_list if str(x).strip()})
        if not ids:
            return {}

        chunk_size = max(1, int(os.getenv("BOT1_METAR_IDS_CHUNK", "15")))
        out: dict[str, MetarObservation] = {}

        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            merged = self._fetch_chunk(chunk)
            out.update(merged)

        missing = set(ids) - set(out.keys())
        if missing:
            LOG.warning("METAR sans température ou absents pour: %s", ", ".join(sorted(missing)))
        return out

    def _fetch_chunk(self, chunk: list[str]) -> dict[str, MetarObservation]:
        q = urllib.parse.urlencode(
            {
                "ids": ",".join(chunk),
                "format": "json",
                "hours": str(self.hours),
            }
        )
        url = f"{AWC_METAR_BASE}?{q}"
        headers = {
            "User-Agent": os.getenv("BOT1_METAR_USER_AGENT", "POLYMARKET-bot1-rt-metar/1.0"),
            "Accept": "application/json",
        }
        timeout = httpx.Timeout(self.timeout_sec)
        last_exc: Exception | None = None
        raw_bytes: bytes | None = None
        for attempt in range(self.max_retries):
            try:
                with httpx.Client(timeout=timeout, trust_env=True) as client:
                    r = client.get(url, headers=headers)
                    r.raise_for_status()
                    raw_bytes = r.content
                break
            except (httpx.TimeoutException, httpx.HTTPError, httpx.RequestError) as e:
                last_exc = e
                LOG.warning(
                    "METAR tentative %s/%s échouée (%s): %s",
                    attempt + 1,
                    self.max_retries,
                    type(e).__name__,
                    e,
                )
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_backoff_sec * (attempt + 1))
        if raw_bytes is None:
            assert last_exc is not None
            LOG.error("METAR indisponible après %s tentatives: %s", self.max_retries, last_exc)
            raise last_exc

        data = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(data, list):
            LOG.error("Réponse METAR inattendue (pas une liste): %s", type(data).__name__)
            return {}
        return _pick_best_per_station(data)
