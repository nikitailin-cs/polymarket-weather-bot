#!/usr/bin/env python3
"""
Refresh ``data/icao_mapping.json`` from Polymarket's daily temperature events.

    python bots/metar_ws/refresh_mapping.py            # write the mapping
    python bots/metar_ws/refresh_mapping.py --dry-run  # print what would change

Gamma ``/events?tag_slug=weather&closed=false`` lists events such as
"Highest temperature in NYC on October 25?" whose child markets are buckets
("60°F or below", "61°F", ..., "70°F or higher"). For each bucket market the
mapping entry carries the resolution station (ICAO = last path segment of the
Wunderground ``resolutionSource`` URL), the bucket as ``threshold_low`` /
``threshold_high`` in the market unit, the station timezone (from
``data/metar_ws_stations.json``) and the target local date.

Only stations served by METAR.ws are kept (others are listed in the summary).
Existing entries for past dates are dropped; manual entries (no ``source``
field) are preserved.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

LOG = logging.getLogger("refresh_mapping")

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}
_TITLE = re.compile(r"^(highest|lowest)\s+temperature\s+in\s+(.+?)\s+on\s+([A-Za-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?\??$", re.I)
_NUM = r"([-+]?\d+(?:\.\d+)?)"
_DEG = r"\s*[°º]?\s*([CF])\b"
_BELOW = re.compile(rf"^{_NUM}{_DEG}\s+or\s+(?:below|lower|less)$", re.I)
_ABOVE = re.compile(rf"^{_NUM}{_DEG}\s+or\s+(?:higher|above|more)$", re.I)
_RANGE = re.compile(rf"^{_NUM}\s*[-–]\s*{_NUM}{_DEG}$", re.I)
_EXACT = re.compile(rf"^{_NUM}{_DEG}$", re.I)


def parse_bucket(label: str) -> Optional[dict[str, Any]]:
    """``"16°C"`` -> [16,16]; ``"15°C or below"`` -> [None,15]; ``"60-61°F"`` -> [60,61]; ``"70°F or higher"`` -> [70,None]."""
    s = (label or "").strip().replace("°", "°")
    m = _BELOW.match(s)
    if m:
        return {"low": None, "high": float(m.group(1)), "unit": m.group(2).upper()}
    m = _ABOVE.match(s)
    if m:
        return {"low": float(m.group(1)), "high": None, "unit": m.group(2).upper()}
    m = _RANGE.match(s)
    if m:
        return {"low": float(m.group(1)), "high": float(m.group(2)), "unit": m.group(3).upper()}
    m = _EXACT.match(s)
    if m:
        v = float(m.group(1))
        return {"low": v, "high": v, "unit": m.group(2).upper()}
    return None


def parse_title(title: str, end_date: Optional[str]) -> Optional[dict[str, Any]]:
    m = _TITLE.match((title or "").strip())
    if not m:
        return None
    kind, city, month, day, year = m.groups()
    mon = MONTHS.get(month.lower())
    if not mon:
        return None
    if year:
        y = int(year)
    else:
        end = _parse_dt(end_date)
        y = end.year if end else datetime.now(timezone.utc).year
    try:
        target = date(y, mon, int(day))
    except ValueError:
        return None
    return {"variable": "temp_min" if kind.lower() == "lowest" else "temp_max", "city": city.strip(), "target_date": target.isoformat()}


def icao_from_resolution_source(url: str) -> str:
    """``...metar?ids=KLGA&format=raw...`` -> ``KLGA``; ``.../history/daily/cn/jinan/ZSJN`` -> ``ZSJN``."""
    u = (url or "").strip()
    if not u:
        return ""
    try:
        q = parse_qs(urlparse(u).query)
    except ValueError:
        q = {}
    ids = q.get("ids") or []
    if ids:
        code = ids[0].split(",")[0].strip().upper()
        return code if re.fullmatch(r"[A-Z0-9]{4}", code) else ""
    seg = urlparse(u).path.rstrip("/").split("/")[-1].upper()
    return seg if re.fullmatch(r"[A-Z0-9]{4}", seg) else ""


def _parse_dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def served_stations() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads((_ROOT / "data" / "metar_ws_stations.json").read_text(encoding="utf-8"))
        return {k.upper(): v for k, v in (data.get("stations") or {}).items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def fetch_weather_events(base_url: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
    import httpx

    base = (base_url or os.getenv("GAMMA_API_URL") or "https://gamma-api.polymarket.com").rstrip("/")
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        page: Any = None
        last: Optional[Exception] = None
        for attempt in range(4):
            try:
                r = httpx.get(f"{base}/events", params={"tag_slug": "weather", "closed": "false", "limit": limit, "offset": offset}, timeout=40.0)
                r.raise_for_status()
                page = r.json() or []
                break
            except Exception as e:  # noqa: BLE001 - transient proxy / TLS timeouts
                last = e
                LOG.warning("gamma events offset=%s attempt %s failed: %s", offset, attempt + 1, e)
                time.sleep(2.0 * (attempt + 1))
        if page is None:
            raise RuntimeError(f"gamma events unavailable: {last}")
        out.extend(e for e in page if isinstance(e, dict))
        if len(page) < limit:
            break
        offset += limit
    return out


def build_entries(events: list[dict[str, Any]], served: dict[str, dict[str, Any]], *, today: Optional[date] = None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    today = today or datetime.now(timezone.utc).date()
    entries: dict[str, dict[str, Any]] = {}
    stats = {"events": 0, "temperature_events": 0, "markets": 0, "kept": 0, "unserved": {}, "unparsed": 0, "past": 0, "no_station": 0}
    for ev in events:
        stats["events"] += 1
        info = parse_title(str(ev.get("title") or ""), ev.get("endDate"))
        if not info:
            continue
        stats["temperature_events"] += 1
        if date.fromisoformat(info["target_date"]) < today - timedelta(days=1):
            stats["past"] += 1
            continue
        for m in ev.get("markets") or []:
            if not isinstance(m, dict):
                continue
            stats["markets"] += 1
            cid = str(m.get("conditionId") or "").strip()
            if not cid:
                continue
            icao = icao_from_resolution_source(str(m.get("resolutionSource") or ev.get("resolutionSource") or ""))
            if not icao:
                stats["no_station"] += 1
                continue
            if icao not in served:
                stats["unserved"][icao] = info["city"]
                continue
            bucket = parse_bucket(str(m.get("groupItemTitle") or ""))
            if not bucket:
                stats["unparsed"] += 1
                continue
            tokens = m.get("clobTokenIds")
            try:
                token_list = json.loads(tokens) if isinstance(tokens, str) else (tokens or [])
            except ValueError:
                token_list = []
            entries[cid] = {
                "name": f"{info['city']} {'high' if info['variable'] == 'temp_max' else 'low'} {m.get('groupItemTitle')} {info['target_date']}",
                "yes_token_id": str(token_list[0]) if len(token_list) >= 2 else None,
                "no_token_id": str(token_list[1]) if len(token_list) >= 2 else None,
                "question": m.get("question"),
                "event_slug": ev.get("slug"),
                "market_slug": m.get("slug"),
                "icao": icao,
                "timezone": served[icao].get("timezone") or "UTC",
                "weather_variable": info["variable"],
                "threshold_unit": bucket["unit"],
                "threshold_low": bucket["low"],
                "threshold_high": bucket["high"],
                # legacy single-threshold fields for tools that still read them
                "threshold": bucket["low"] if bucket["low"] is not None else bucket["high"],
                "yes_if_above": bucket["high"] is None,
                "target_date": info["target_date"],
                "deadline_local_hhmm": "23:59",
                "dead_min_gap": 15,
                "resolution_source": m.get("resolutionSource"),
                "source": "gamma",
                "refreshed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            stats["kept"] += 1
    return entries, stats


def merge_mapping(existing: dict[str, Any], fresh: dict[str, dict[str, Any]], *, today: Optional[date] = None) -> dict[str, Any]:
    """Keep notes and manual entries, drop gamma entries older than yesterday, overwrite with fresh."""
    today = today or datetime.now(timezone.utc).date()
    out: dict[str, Any] = {}
    for k, v in existing.items():
        if k.startswith("_") or not isinstance(v, dict):
            out[k] = v
            continue
        if v.get("source") == "gamma":
            try:
                if date.fromisoformat(str(v.get("target_date"))) < today - timedelta(days=1):
                    continue
            except ValueError:
                continue
        out[k] = v
    out.update(fresh)
    return out


def mapping_path() -> Path:
    from core.bot2_data_paths import get_rt_paths

    return get_rt_paths()["mapping"]


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh icao_mapping.json from Polymarket daily temperature events")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mapping", default="", help="mapping path (default: the bot2 RT mapping)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    path = Path(args.mapping) if args.mapping else mapping_path()
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError:
        existing = {}
    served = served_stations()
    events = fetch_weather_events()
    fresh, stats = build_entries(events, served)
    merged = merge_mapping(existing if isinstance(existing, dict) else {}, fresh)
    LOG.info(
        "events=%s temperature=%s markets=%s kept=%s unparsed=%s past=%s no_station=%s",
        stats["events"], stats["temperature_events"], stats["markets"], stats["kept"], stats["unparsed"], stats["past"], stats["no_station"],
    )
    if stats["unserved"]:
        LOG.info("stations not served by METAR.ws (skipped): %s", ", ".join(f"{k} ({v})" for k, v in sorted(stats["unserved"].items())))
    by_station: dict[str, int] = {}
    for v in fresh.values():
        by_station[v["icao"]] = by_station.get(v["icao"], 0) + 1
    LOG.info("buckets per station: %s", ", ".join(f"{k}:{n}" for k, n in sorted(by_station.items())))
    n_markets = sum(1 for k, v in merged.items() if not k.startswith("_") and isinstance(v, dict))
    if args.dry_run:
        LOG.info("dry-run: mapping would hold %s market(s) at %s", n_markets, path)
        return 0
    write_atomic(path, merged)
    LOG.info("written %s market(s) to %s", n_markets, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
