#!/usr/bin/env python3
"""
Synchronise un référentiel stations WMO dans la table SQLite `stations_wmo`.

Objectif:
  - télécharger un CSV WMO (ou CSV combiné WMO/ICAO),
  - extraire wmo_id, latitude, longitude, elevation_m,
  - stocker dans `master_stations.sqlite3` sans écraser la table `stations`.

Par défaut, la source est un CSV NOAA/NCEI maintenu et léger; le parseur accepte
plusieurs alias de colonnes afin de rester compatible avec d'autres exports WMO.
"""

from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

_ROOT = Path(__file__).resolve().parents[4]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from monitoring.error_guard import run_guarded  # noqa: E402

DEFAULT_SOURCE_URL = "https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv"
DEFAULT_DB_PATH = Path("data/master_stations.sqlite3")
DEFAULT_TIMEOUT_SEC = 60.0

# Alias possibles selon la source CSV
WMO_COLUMNS = ("WMO_ID", "WMOID", "WMO", "WMO ID")
USAF_COLUMNS = ("USAF", "usaf")
LAT_COLUMNS = ("LATITUDE", "LAT", "latitude", "lat")
LON_COLUMNS = ("LONGITUDE", "LON", "longitude", "lon")
ELEV_COLUMNS = ("ELEVATION", "ELEV_M", "ELEV", "elevation", "elevation_m")
BEGIN_COLUMNS = ("BEGIN", "begin", "START", "start_date")
END_COLUMNS = ("END", "end", "STOP", "end_date")


def _download_text(url: str, timeout_sec: float) -> str:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "POLYMARKET-sync_wmo_stations/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        data = resp.read()
    return data.decode("utf-8", errors="replace")


def _pick_first(row: dict[str, str], keys: Iterable[str]) -> Optional[str]:
    for k in keys:
        if k in row:
            v = (row.get(k) or "").strip()
            if v:
                return v
    return None


def _norm_wmo_id(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = "".join(ch for ch in raw if ch.isdigit())
    # WMO synop attendu: 5 chiffres.
    # Défense: certaines sources peuvent perdre le zero initial (ex: 07156 -> 7156).
    if 1 <= len(s) <= 5:
        return s.zfill(5)
    return None


def _wmo_from_usaf(raw_usaf: Optional[str]) -> Optional[str]:
    """
    NOAA ISD history expose souvent un identifiant USAF (6 digits).
    Quand un code WMO existe, il correspond généralement aux 5 premiers digits.
    """
    if not raw_usaf:
        return None
    s = "".join(ch for ch in raw_usaf if ch.isdigit())
    if len(s) != 6 or s == "999999":
        return None
    return _norm_wmo_id(s[:5])


def _parse_float(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_yyyymmdd(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return int(digits[:8])
    except ValueError:
        return None


def _iter_wmo_rows(csv_text: str, min_end_year: int):
    reader = csv.DictReader(io.StringIO(csv_text))
    best_by_wmo: dict[str, tuple[int, int, float, float, Optional[float]]] = {}
    for row in reader:
        wmo = _norm_wmo_id(_pick_first(row, WMO_COLUMNS))
        if not wmo:
            wmo = _wmo_from_usaf(_pick_first(row, USAF_COLUMNS))
        if not wmo:
            continue
        lat = _parse_float(_pick_first(row, LAT_COLUMNS))
        lon = _parse_float(_pick_first(row, LON_COLUMNS))
        elev = _parse_float(_pick_first(row, ELEV_COLUMNS))
        if lat is None or lon is None:
            continue
        begin_i = _parse_yyyymmdd(_pick_first(row, BEGIN_COLUMNS)) or 0
        end_i = _parse_yyyymmdd(_pick_first(row, END_COLUMNS)) or 0
        end_year = end_i // 10000 if end_i > 0 else 0
        if end_year and end_year < min_end_year:
            continue
        prev = best_by_wmo.get(wmo)
        # En cas de doublon WMO: on garde l'enregistrement le plus récent.
        if prev is None or (end_i, begin_i) > (prev[0], prev[1]):
            best_by_wmo[wmo] = (end_i, begin_i, lat, lon, elev)
    for wmo, (_, __, lat, lon, elev) in best_by_wmo.items():
        yield wmo, lat, lon, elev


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stations_wmo (
            wmo_id TEXT PRIMARY KEY,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            elevation_m REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stations_wmo_lat_lon ON stations_wmo(latitude, longitude)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def sync_wmo_stations(
    source_url: str, db_path: Path, timeout_sec: float, min_end_year: int
) -> tuple[int, int]:
    csv_text = _download_text(source_url, timeout_sec=timeout_sec)
    rows = list(_iter_wmo_rows(csv_text, min_end_year=min_end_year))

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        _init_db(conn)
        with conn:
            conn.execute("DELETE FROM stations_wmo")
            conn.executemany(
                """
                INSERT INTO stations_wmo(wmo_id, latitude, longitude, elevation_m)
                VALUES(?, ?, ?, ?)
                """,
                rows,
            )
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES('wmo_source_url', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (source_url,),
            )
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES('wmo_row_count', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(len(rows)),),
            )
    finally:
        conn.close()

    return len(csv_text), len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync stations WMO vers table SQLite stations_wmo."
    )
    parser.add_argument(
        "--source-url",
        default=DEFAULT_SOURCE_URL,
        help=f"CSV source URL (défaut: {DEFAULT_SOURCE_URL})",
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"Chemin SQLite (défaut: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"Timeout HTTP en secondes (défaut: {DEFAULT_TIMEOUT_SEC})",
    )
    parser.add_argument(
        "--min-end-year",
        type=int,
        default=datetime.now(timezone.utc).year,
        help="Filtre END >= année donnée (défaut: année UTC courante).",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    try:
        txt_size, row_count = sync_wmo_stations(
            source_url=args.source_url,
            db_path=db_path,
            timeout_sec=args.timeout_sec,
            min_end_year=int(args.min_end_year),
        )
    except Exception as exc:
        print(f"[ERREUR] sync_wmo_stations: {exc}", file=sys.stderr)
        return 1

    print(f"[OK] Source: {args.source_url}")
    print(f"[OK] CSV téléchargé: {txt_size} caractères")
    print(f"[OK] Stations WMO importées: {row_count}")
    print(f"[OK] Filtre END >= {int(args.min_end_year)}")
    print(f"[OK] SQLite: {db_path} (table stations_wmo)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_guarded("bot1.sync_wmo_stations", main))
