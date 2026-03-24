#!/usr/bin/env python3
"""
Synchronise la liste maître des stations NOAA ADDS (source de vérité) vers SQLite.

Source:
  https://aviationweather.gov/data/cache/stations.cache.xml.gz

Pipeline:
  1) Téléchargement binaire (gzip)
  2) Décompression en RAM
  3) Parsing XML
  4) Extraction station_id, latitude, longitude, elevation
  5) Écriture transactionnelle dans une base SQLite indexée
"""

from __future__ import annotations

import argparse
import gzip
import io
import sqlite3
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

DEFAULT_SOURCE_URL = "https://aviationweather.gov/data/cache/stations.cache.xml.gz"
DEFAULT_DB_PATH = Path("data/master_stations.sqlite3")
DEFAULT_TIMEOUT_SEC = 60.0


def _download_bytes(url: str, timeout_sec: float) -> bytes:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "POLYMARKET-sync_master_stations/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return resp.read()


def _local_name(tag: str) -> str:
    # Gère les tags namespacés: "{ns}station" -> "station"
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _find_text(node: ET.Element, *tags: str) -> Optional[str]:
    wanted = {t.lower() for t in tags}
    for child in list(node):
        lname = _local_name(child.tag).lower()
        if lname in wanted and child.text:
            txt = child.text.strip()
            if txt:
                return txt
    return None


def _parse_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _iter_station_rows(xml_bytes: bytes):
    root = ET.fromstring(xml_bytes)

    for st in root.iter():
        if _local_name(st.tag).lower() != "station":
            continue
        station_id = _find_text(st, "station_id", "icaoId", "icao_id")
        lat_s = _find_text(st, "latitude", "lat")
        lon_s = _find_text(st, "longitude", "lon", "long")
        elev_s = _find_text(st, "elevation_m", "elevation", "elev")

        if not station_id:
            continue

        lat = _parse_float(lat_s)
        lon = _parse_float(lon_s)
        elev = _parse_float(elev_s)

        if lat is None or lon is None:
            continue

        yield station_id.upper(), lat, lon, elev


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stations (
            station_id TEXT PRIMARY KEY,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            elevation_m REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stations_lat_lon ON stations(latitude, longitude)"
    )


def sync_master_stations(
    source_url: str,
    db_path: Path,
    timeout_sec: float,
) -> tuple[int, int]:
    gz_bytes = _download_bytes(source_url, timeout_sec=timeout_sec)

    with gzip.GzipFile(fileobj=io.BytesIO(gz_bytes), mode="rb") as gz:
        xml_bytes = gz.read()

    rows = list(_iter_station_rows(xml_bytes))

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        _init_db(conn)
        with conn:
            conn.execute("DELETE FROM stations")
            conn.executemany(
                """
                INSERT INTO stations(station_id, latitude, longitude, elevation_m)
                VALUES(?, ?, ?, ?)
                """,
                rows,
            )
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES('source_url', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (source_url,),
            )
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES('row_count', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(len(rows)),),
            )
    finally:
        conn.close()

    return len(gz_bytes), len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync ADDS stations.cache.xml.gz vers SQLite"
    )
    parser.add_argument(
        "--source-url",
        default=DEFAULT_SOURCE_URL,
        help=f"URL source (defaut: {DEFAULT_SOURCE_URL})",
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"Chemin SQLite (defaut: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"Timeout HTTP en secondes (defaut: {DEFAULT_TIMEOUT_SEC})",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)

    try:
        gz_size, row_count = sync_master_stations(
            source_url=args.source_url,
            db_path=db_path,
            timeout_sec=args.timeout_sec,
        )
    except Exception as exc:
        print(f"[ERREUR] sync_master_stations: {exc}", file=sys.stderr)
        return 1

    print(f"[OK] Source: {args.source_url}")
    print(f"[OK] gzip telecharge: {gz_size} octets")
    print(f"[OK] Stations importees: {row_count}")
    print(f"[OK] SQLite: {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
