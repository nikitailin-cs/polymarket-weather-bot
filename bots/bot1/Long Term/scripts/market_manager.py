#!/usr/bin/env python3
"""
Liaison JSON <-> SQLite pour les marchés météo actifs.

Entrées:
  - JSON de registre (data/active_markets.json)
  - SQLite des stations (data/master_stations.sqlite3)

Sortie:
  - Structures enrichies avec coordonnées station (si ICAO trouvé)
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

# Règles de cohérence géographique minimales (sanity check).
SANITY_HINT_BBOXES: dict[str, tuple[float, float, float, float]] = {
    # (lat_min, lat_max, lon_min, lon_max)
    "hong kong": (21.5, 23.0, 113.5, 114.6),
    "paris": (48.0, 49.3, 1.5, 3.5),
    "london": (51.0, 52.0, -1.0, 1.0),
    "new york": (40.2, 41.2, -74.5, -72.5),
}


@dataclass(frozen=True)
class ActiveMarket:
    market_id: str
    station_code: str
    oracle_source: str
    question: str
    operator: str
    variable: str
    unit: str
    threshold_range: Optional[list[float]]
    oracle_rounding: int
    threshold: float


@dataclass(frozen=True)
class StationInfo:
    station_id: str
    latitude: float
    longitude: float
    elevation_m: Optional[float]


@dataclass(frozen=True)
class EnrichedMarket:
    market_id: str
    station_code: str
    oracle_source: str
    question: str
    operator: str
    variable: str
    unit: str
    threshold_range: Optional[list[float]]
    oracle_rounding: int
    threshold: float
    station_info: Optional[StationInfo]


@dataclass(frozen=True)
class MarketTarget:
    """Objet cible final pour le moteur: ID + coordonnees + seuil."""

    market_id: str
    station_code: str
    oracle_source: str
    question: str
    operator: str
    variable: str
    unit: str
    threshold_range: Optional[list[float]]
    oracle_rounding: int
    latitude: float
    longitude: float
    threshold: float


def repo_root_from_this_file() -> Path:
    # .../POLYMARKET/bots/bot1/Long Term/scripts/market_manager.py -> .../POLYMARKET
    return Path(__file__).resolve().parents[4]


def default_markets_json_path() -> Path:
    return repo_root_from_this_file() / "data" / "active_markets.json"


def default_master_stations_db_path() -> Path:
    return repo_root_from_this_file() / "data" / "master_stations.sqlite3"


def _require_dict(obj: Any, ctx: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError(f"{ctx}: objet JSON attendu")
    return obj


def _looks_like_icao(code: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{4}", code))


def _normalize_wmo_id(code: str) -> Optional[str]:
    digits = "".join(ch for ch in (code or "") if ch.isdigit())
    if 1 <= len(digits) <= 5:
        return digits.zfill(5)
    return None


def _passes_sanity_check(question: str, lat: float, lon: float) -> bool:
    q = (question or "").lower()
    for hint, (la0, la1, lo0, lo1) in SANITY_HINT_BBOXES.items():
        if hint in q:
            return la0 <= lat <= la1 and lo0 <= lon <= lo1
    return True


def load_active_markets(json_path: Path) -> list[ActiveMarket]:
    if not json_path.is_file():
        raise FileNotFoundError(f"Registre JSON introuvable: {json_path}")

    payload = _require_dict(json.loads(json_path.read_text(encoding="utf-8")), "racine")
    items = payload.get("markets")
    if not isinstance(items, list):
        raise ValueError("active_markets.json: la clé 'markets' doit être une liste")

    out: list[ActiveMarket] = []
    for i, raw in enumerate(items):
        obj = _require_dict(raw, f"markets[{i}]")
        market_id = str(obj.get("market_id", "")).strip()
        oracle_source = str(obj.get("oracle_source", "unknown")).strip().lower()
        question = str(obj.get("question", "")).strip()
        # Nomenclature ContractSpec: operator=gt/gte/lt/lte/eq/between, variable normalisée.
        operator = str(obj.get("operator", "")).strip()
        variable = str(obj.get("variable", "")).strip()
        unit = str(obj.get("unit", "")).strip().lower()
        threshold_range_raw = obj.get("threshold_range")
        threshold_range: Optional[list[float]] = None
        if isinstance(threshold_range_raw, list) and len(threshold_range_raw) == 2:
            try:
                threshold_range = [float(threshold_range_raw[0]), float(threshold_range_raw[1])]
            except (TypeError, ValueError):
                threshold_range = None
        try:
            oracle_rounding = int(obj.get("oracle_rounding", 1))
        except (TypeError, ValueError):
            oracle_rounding = 1
        # Compat: accepte "station" (nouveau) ou "icao" (historique).
        station = str(obj.get("station") or obj.get("icao") or "").strip().upper()
        thr_raw = obj.get("threshold")
        try:
            threshold = float(thr_raw)
        except (TypeError, ValueError):
            raise ValueError(f"markets[{i}].threshold invalide: {thr_raw!r}") from None
        if not market_id:
            raise ValueError(f"markets[{i}].market_id vide")
        if not station:
            raise ValueError(f"markets[{i}].station/icao vide")
        if not operator:
            raise ValueError(f"markets[{i}].operator vide")
        if not variable:
            raise ValueError(f"markets[{i}].variable vide")
        if not unit:
            raise ValueError(f"markets[{i}].unit vide")
        out.append(
            ActiveMarket(
                market_id=market_id,
                station_code=station,
                oracle_source=oracle_source,
                question=question,
                operator=operator,
                variable=variable,
                unit=unit,
                threshold_range=threshold_range,
                oracle_rounding=oracle_rounding,
                threshold=threshold,
            )
        )
    return out


def get_station_by_icao(conn: sqlite3.Connection, icao: str) -> Optional[StationInfo]:
    row = conn.execute(
        """
        SELECT station_id, latitude, longitude, elevation_m
        FROM stations
        WHERE station_id = ?
        """,
        (icao.upper(),),
    ).fetchone()
    if row is None:
        return None
    return StationInfo(
        station_id=str(row[0]),
        latitude=float(row[1]),
        longitude=float(row[2]),
        elevation_m=float(row[3]) if row[3] is not None else None,
    )


def get_station_lat_lon_by_icao(conn: sqlite3.Connection, icao: str) -> Optional[tuple[float, float]]:
    """
    Requete minimale demandee:
      SELECT latitude, longitude FROM stations WHERE station_id = ?
    """
    row = conn.execute(
        "SELECT latitude, longitude FROM stations WHERE station_id = ?",
        (icao.upper(),),
    ).fetchone()
    if row is None:
        return None
    return float(row[0]), float(row[1])


def get_station_lat_lon_by_wmo(conn: sqlite3.Connection, wmo_id: str) -> Optional[tuple[float, float]]:
    wmo_norm = _normalize_wmo_id(wmo_id)
    if not wmo_norm:
        return None
    row = conn.execute(
        "SELECT latitude, longitude FROM stations_wmo WHERE wmo_id = ?",
        (wmo_norm,),
    ).fetchone()
    if row is None:
        return None
    return float(row[0]), float(row[1])


def _is_icao_source(source: str) -> bool:
    s = (source or "").lower()
    return ("icao" in s) or ("adds" in s) or ("aviation" in s)


def _is_wmo_source(source: str) -> bool:
    s = (source or "").lower()
    return "wmo" in s


def _is_ghcnd_source(source: str) -> bool:
    s = (source or "").lower()
    return "ghcnd" in s


def resolve_station_lat_lon_strict(
    conn: sqlite3.Connection,
    market: ActiveMarket,
) -> tuple[Optional[tuple[float, float]], str]:
    """
    Strict resolver:
      - CAS A ICAO: source contient icao/adds/aviation -> table stations
      - CAS B WMO: source contient wmo -> table stations_wmo
      - CAS C GHCND/non géré: rejet immédiat
      - aucun fallback croisé autorisé
    """
    src = (market.oracle_source or "unknown").lower()
    code = market.station_code

    if _is_icao_source(src):
        if not _looks_like_icao(code):
            return None, f"id ICAO invalide: {code}"
        ll = get_station_lat_lon_by_icao(conn, code)
        if ll is None:
            return None, f"id ICAO introuvable: {code}"
        return ll, "ok_icao"

    if _is_wmo_source(src):
        ll = get_station_lat_lon_by_wmo(conn, code)
        if ll is None:
            return None, f"id WMO introuvable: {code}"
        return ll, "ok_wmo"

    if _is_ghcnd_source(src):
        return None, f"source ghcnd non supportée sans table dédiée: {src}"

    return None, f"source non gérée: {src}"


def enrich_markets_with_station(
    markets: Iterable[ActiveMarket],
    master_stations_db_path: Path,
) -> list[EnrichedMarket]:
    if not master_stations_db_path.is_file():
        raise FileNotFoundError(f"SQLite stations introuvable: {master_stations_db_path}")

    conn = sqlite3.connect(str(master_stations_db_path))
    try:
        enriched: list[EnrichedMarket] = []
        for m in markets:
            st = get_station_by_icao(conn, m.station_code)
            enriched.append(
                EnrichedMarket(
                    market_id=m.market_id,
                    station_code=m.station_code,
                    oracle_source=m.oracle_source,
                    question=m.question,
                    operator=m.operator,
                    variable=m.variable,
                    unit=m.unit,
                    threshold_range=m.threshold_range,
                    oracle_rounding=m.oracle_rounding,
                    threshold=m.threshold,
                    station_info=st,
                )
            )
        return enriched
    finally:
        conn.close()


def build_market_targets(
    json_path: Optional[Path] = None,
    master_stations_db_path: Optional[Path] = None,
) -> list[MarketTarget]:
    """
    Jointure JSON <-> SQLite pour le bot:
      1) lit active_markets.json
      2) fait la requete lat/lon par ICAO
      3) compile une liste de MarketTarget

    Securite:
      - si ICAO absent de SQLite: alerte console et on continue.
    """
    jp = json_path if json_path is not None else default_markets_json_path()
    db = (
        master_stations_db_path
        if master_stations_db_path is not None
        else default_master_stations_db_path()
    )

    markets = load_active_markets(jp)
    if not db.is_file():
        raise FileNotFoundError(f"SQLite stations introuvable: {db}")

    targets: list[MarketTarget] = []
    conn = sqlite3.connect(str(db))
    try:
        for m in markets:
            lat_lon, reason = resolve_station_lat_lon_strict(conn, m)
            if lat_lon is None:
                logging.warning(
                    "Marché %s ignoré : Source %s ou ID %s introuvable. (%s)",
                    m.market_id,
                    m.oracle_source,
                    m.station_code,
                    reason,
                )
                continue
            lat, lon = lat_lon
            if not _passes_sanity_check(m.question, lat, lon):
                print(
                    f"[ALERTE] sanity check géographique échoué: market_id={m.market_id} station={m.station_code} lat={lat:.4f} lon={lon:.4f}",
                    flush=True,
                )
                continue
            targets.append(
                MarketTarget(
                    market_id=m.market_id,
                    station_code=m.station_code,
                    oracle_source=m.oracle_source,
                    question=m.question,
                    operator=m.operator,
                    variable=m.variable,
                    unit=m.unit,
                    threshold_range=m.threshold_range,
                    oracle_rounding=m.oracle_rounding,
                    latitude=lat,
                    longitude=lon,
                    threshold=m.threshold,
                )
            )
    finally:
        conn.close()
    return targets
