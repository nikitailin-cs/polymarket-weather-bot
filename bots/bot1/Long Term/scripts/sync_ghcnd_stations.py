#!/usr/bin/env python3
"""
Synchronise le référentiel NOAA GHCND (stations) vers SQLite.

Source officielle:
  https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt

Format source:
  Fichier fixed-width (largeur fixe), pas un CSV.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Optional

DEFAULT_SOURCE_URL = "https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt"
DEFAULT_DB_PATH = Path("data/master_stations.sqlite3")
DEFAULT_TIMEOUT_SEC = 60.0


def _download_text(url: str, timeout_sec: float) -> str:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "POLYMARKET-sync_ghcnd_stations/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        data = resp.read()
    return data.decode("utf-8", errors="replace")


def _parse_float(raw: str) -> Optional[float]:
    txt = (raw or "").strip()
    if not txt:
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def _normalize_elevation_m(elev: Optional[float]) -> Optional[float]:
    # NOAA GHCND utilise -999.9 comme sentinelle "altitude inconnue".
    if elev is None:
        return None
    if elev == -999.9:
        return None
    return elev


def _iter_ghcnd_rows(txt: str):
    for line in txt.splitlines():
        if not line.strip():
            continue
        if len(line) < 38:
            continue

        # Fixed-width NOAA GHCND
        # 0:11   -> station id (11 chars)
        # 12:20  -> latitude
        # 21:30  -> longitude
        # 31:37  -> elevation
        ghcnd_id = line[0:11].strip()
        lat = _parse_float(line[12:20])
        lon = _parse_float(line[21:30])
        elev = _normalize_elevation_m(_parse_float(line[31:37]))

        if len(ghcnd_id) != 11:
            continue
        if lat is None or lon is None:
            continue

        yield ghcnd_id, lat, lon, elev


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stations_ghcnd (
            ghcnd_id TEXT PRIMARY KEY,
            latitude REAL,
            longitude REAL,
            elevation_m REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stations_ghcnd_lat_lon ON stations_ghcnd(latitude, longitude)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def sync_ghcnd_stations(source_url: str, db_path: Path, timeout_sec: float) -> tuple[int, int]:
    text = _download_text(source_url, timeout_sec=timeout_sec)
    rows = list(_iter_ghcnd_rows(text))

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        _init_db(conn)
        with conn:
            conn.execute("DELETE FROM stations_ghcnd")
            conn.executemany(
                """
                INSERT INTO stations_ghcnd(ghcnd_id, latitude, longitude, elevation_m)
                VALUES(?, ?, ?, ?)
                """,
                rows,
            )
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES('ghcnd_source_url', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (source_url,),
            )
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES('ghcnd_row_count', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(len(rows)),),
            )
    finally:
        conn.close()

    return len(text), len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync stations GHCND vers table SQLite stations_ghcnd."
    )
    parser.add_argument(
        "--source-url",
        default=DEFAULT_SOURCE_URL,
        help=f"URL source (défaut: {DEFAULT_SOURCE_URL})",
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
    args = parser.parse_args()

    db_path = Path(args.db_path)
    try:
        txt_size, row_count = sync_ghcnd_stations(
            source_url=args.source_url,
            db_path=db_path,
            timeout_sec=args.timeout_sec,
        )
    except Exception as exc:
        print(f"[ERREUR] sync_ghcnd_stations: {exc}", file=sys.stderr)
        return 1

    print(f"[OK] Source: {args.source_url}")
    print(f"[OK] Fichier téléchargé: {txt_size} caractères")
    print(f"[OK] Stations GHCND importées: {row_count}")
    print(f"[OK] SQLite: {db_path} (table stations_ghcnd)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
