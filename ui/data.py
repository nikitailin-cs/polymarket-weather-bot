"""
Data assembly for the dashboard. Pure functions over the files the daemon and
the RT cycle already write; nothing here talks to the trading path.

Sources:

- METAR.ws snapshot      ``core.metar_ws_snapshot.snapshot_path()``
- served station list    ``data/metar_ws_stations.json`` (report minutes)
- market mapping         ``data/icao_mapping.json`` (or the pipeline's path)
- RT signals + manifest  ``realtime_signals.json`` / ``realtime_signals_meta.json``
- Polymarket prices      Gamma API (optional, cached, failures tolerated)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from core.metar_observation import celsius_to_fahrenheit, parse_utc
from core.metar_ws_snapshot import read_snapshot, snapshot_path

LOG = logging.getLogger("ui.data")
_ROOT = Path(__file__).resolve().parent.parent

LATE_GRACE_MIN = 8  # minutes after the expected report minute before a station is "late"


# -- paths ---------------------------------------------------------------------------

def pipeline() -> str:
    return (os.getenv("UPDATE_BOT_RT_PIPELINE") or "bot2").strip().lower()


def mapping_path() -> Path:
    if pipeline() == "bot1":
        from core.bot1_realtime_data_paths import icao_mapping_path

        return icao_mapping_path()
    from core.bot2_data_paths import get_rt_paths

    return get_rt_paths()["mapping"]


def signals_paths() -> tuple[Path, Path]:
    if pipeline() == "bot1":
        from core.bot1_realtime_data_paths import realtime_signals_meta_path, realtime_signals_path

        return realtime_signals_path(), realtime_signals_meta_path()
    from core.bot2_data_paths import get_rt_paths

    p = get_rt_paths()
    return p["signals"], p["meta"]


def stations_file() -> Path:
    return _ROOT / "data" / "metar_ws_stations.json"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        LOG.warning("unreadable %s: %s", path, e)
        return None


def served_stations(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    data = _load_json(path or stations_file())
    if not isinstance(data, dict) or not isinstance(data.get("stations"), dict):
        return {}
    return {str(k).upper(): v for k, v in data["stations"].items() if isinstance(v, dict)}


# -- stream health -------------------------------------------------------------------

def next_report_after(last_obs: datetime, minutes: list[int]) -> Optional[datetime]:
    """First scheduled report time strictly after ``last_obs`` (UTC, minute marks)."""
    if not minutes:
        return None
    base = last_obs.replace(second=0, microsecond=0)
    for h in range(0, 3):
        for m in sorted(minutes):
            cand = base.replace(minute=0) + timedelta(hours=h, minutes=m)
            if cand > last_obs:
                return cand
    return None


def station_freshness(
    icao: str,
    obs_row: Optional[dict[str, Any]],
    served: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    info = served.get(icao, {})
    minutes = [int(m) for m in info.get("report_minutes", []) if isinstance(m, int)]
    last = parse_utc(obs_row.get("observation_time_utc")) if obs_row else None
    received = parse_utc(obs_row.get("received_at_utc")) if obs_row else None
    row: dict[str, Any] = {
        "icao": icao,
        "name": info.get("name", ""),
        "city": info.get("city", ""),
        "served": icao in served,
        "report_minutes": minutes,
        "last_obs_utc": last.strftime("%Y-%m-%dT%H:%M:%SZ") if last else None,
        "age_min": round((now - last).total_seconds() / 60, 1) if last else None,
        "delivery_lag_sec": round((received - last).total_seconds(), 1) if last and received else None,
        "temp_c": obs_row.get("temp_c") if obs_row else None,
        "is_speci": bool(obs_row.get("is_speci")) if obs_row else False,
        "raw": obs_row.get("raw") if obs_row else None,
    }
    if last is None:
        row["status"] = "no_data"
        row["next_expected_utc"] = None
        return row
    nxt = next_report_after(last, minutes)
    row["next_expected_utc"] = nxt.strftime("%Y-%m-%dT%H:%M:%SZ") if nxt else None
    if nxt is None:
        row["status"] = "ok"
    elif now > nxt + timedelta(minutes=LATE_GRACE_MIN):
        row["status"] = "late"
        row["late_min"] = round((now - nxt).total_seconds() / 60, 1)
    else:
        row["status"] = "ok"
    return row


def stream_view(snapshot: Optional[dict[str, Any]], served: dict[str, dict[str, Any]], now: datetime) -> dict[str, Any]:
    if snapshot is None:
        return {"available": False, "connected": False, "stations": [], "client": {}, "generated_at_utc": None}
    client = snapshot.get("client") or {}
    gen = parse_utc(snapshot.get("generated_at_utc"))
    wanted = client.get("wanted_channels") or []
    icaos = sorted({c.split(".")[-1].upper() for c in wanted if isinstance(c, str)} | set(snapshot.get("stations", {}).keys()))
    stations = [station_freshness(i, snapshot.get("stations", {}).get(i), served, now) for i in icaos]
    counts = {"ok": 0, "late": 0, "no_data": 0}
    for s in stations:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {
        "available": True,
        "connected": bool(snapshot.get("connected")),
        "generated_at_utc": snapshot.get("generated_at_utc"),
        "snapshot_age_sec": round((now - gen).total_seconds(), 1) if gen else None,
        "client": client,
        "counts": counts,
        "stations": stations,
    }


# -- station board ---------------------------------------------------------------------

def _local_now(tz: str, now: datetime) -> datetime:
    try:
        return now.astimezone(ZoneInfo(tz or "UTC"))
    except Exception:  # noqa: BLE001
        return now


def station_board(snapshot: Optional[dict[str, Any]], served: dict[str, dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    if snapshot is None:
        return []
    days = snapshot.get("days") or {}
    rows: list[dict[str, Any]] = []
    for icao in sorted(set(days) | set(snapshot.get("stations", {}).keys())):
        by_day = days.get(icao) or {}
        latest_obs = snapshot.get("stations", {}).get(icao) or {}
        tz = next(
            (d.get("timezone") for d in by_day.values() if isinstance(d, dict) and d.get("timezone")),
            served.get(icao, {}).get("timezone") or "UTC",
        )
        local_now = _local_now(tz, now)
        today = local_now.date().isoformat()
        st = by_day.get(today)
        info = served.get(icao, {})
        row: dict[str, Any] = {
            "icao": icao,
            "name": info.get("name", ""),
            "city": info.get("city", ""),
            "timezone": tz,
            "local_time": local_now.strftime("%H:%M"),
            "local_date": today,
            "last_temp_c": latest_obs.get("temp_c"),
            "last_obs_utc": latest_obs.get("observation_time_utc"),
            "last_raw": latest_obs.get("raw"),
            "last_is_speci": bool(latest_obs.get("is_speci")),
            "day": None,
            "other_days": sorted(d for d in by_day if d != today),
        }
        if isinstance(st, dict):
            mx, mn = st.get("max_c"), st.get("min_c")
            row["day"] = {
                "max_c": mx,
                "max_f": round(celsius_to_fahrenheit(mx), 1) if mx is not None else None,
                "max_time_utc": st.get("max_time_utc"),
                "max_source": st.get("max_source"),
                "min_c": mn,
                "min_f": round(celsius_to_fahrenheit(mn), 1) if mn is not None else None,
                "min_time_utc": st.get("min_time_utc"),
                "min_source": st.get("min_source"),
                "obs_count": st.get("obs_count"),
                "speci_count": st.get("speci_count"),
                "lead_max_c": st.get("lead_max_c"),
                "lead_max_time_utc": st.get("lead_max_time_utc"),
                "lead_max_is_speci": bool(st.get("lead_max_is_speci")),
                "lead_min_c": st.get("lead_min_c"),
                "lead_min_time_utc": st.get("lead_min_time_utc"),
                "lead_min_is_speci": bool(st.get("lead_min_is_speci")),
                "samples": [[s[0], s[1], bool(s[2]) if len(s) > 2 else False] for s in st.get("samples") or []],
                "reported_24h_max_c": st.get("reported_24h_max_c"),
                "reported_24h_min_c": st.get("reported_24h_min_c"),
            }
        rows.append(row)
    return rows


# -- markets ---------------------------------------------------------------------------

def load_signals(signals_path: Path, meta_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Normalise bot2 (dict keyed by market) and bot1 (payload with ``signals`` list) shapes."""
    raw = _load_json(signals_path)
    meta = _load_json(meta_path) or {}
    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict) and isinstance(raw.get("signals"), list):
        for row in raw["signals"]:
            if isinstance(row, dict) and row.get("market_id"):
                out[str(row["market_id"])] = row
        meta = {**{k: v for k, v in raw.items() if k != "signals"}, **meta}
    elif isinstance(raw, dict):
        out = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    return out, meta if isinstance(meta, dict) else {}


def _to_unit(value_c: Optional[float], unit: str) -> Optional[float]:
    if value_c is None:
        return None
    return round(celsius_to_fahrenheit(value_c), 1) if unit.upper().startswith("F") else round(value_c, 1)


def market_rows(
    mapping: dict[str, Any],
    signals: dict[str, dict[str, Any]],
    snapshot: Optional[dict[str, Any]],
    prices: dict[str, dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    days = (snapshot or {}).get("days") or {}
    rows: list[dict[str, Any]] = []
    for market_id, cfg in mapping.items():
        if not isinstance(market_id, str) or market_id.startswith("_") or not isinstance(cfg, dict):
            continue
        icao = str(cfg.get("icao") or "").upper()
        unit = str(cfg.get("threshold_unit") or "F").upper()
        tz = str(cfg.get("timezone") or "UTC")
        local_now = _local_now(tz, now)
        today = local_now.date().isoformat()
        var = str(cfg.get("weather_variable") or "temp_max").lower()
        try:
            threshold = float(cfg.get("threshold"))
        except (TypeError, ValueError):
            threshold = None
        sig = signals.get(market_id) or {}
        st = (days.get(icao) or {}).get(today) if icao else None
        extreme_c = None
        if isinstance(st, dict):
            extreme_c = st.get("min_c") if var == "temp_min" else st.get("max_c")
        extreme = _to_unit(extreme_c, unit)
        yes_if_above = bool(cfg.get("yes_if_above", True))
        from core.rt_daily_extrema import Bucket
        from core.station_day_state import resolution_value

        bucket = Bucket.from_mapping(cfg)
        res_val = resolution_value(extreme_c, unit) if extreme_c is not None else None
        lead_c = None
        if isinstance(st, dict):
            lead_c = st.get("lead_min_c") if var == "temp_min" else st.get("lead_max_c")
        lead_val = resolution_value(lead_c, unit) if lead_c is not None else None
        gap = None
        if res_val is not None and bucket is not None:
            # positive gap: bucket not reached yet (max must rise / min must fall);
            # negative gap: the extreme already moved past the bucket (dead)
            rises = var != "temp_min"
            if bucket.contains(res_val):
                gap = 0.0
            elif bucket.low is not None and res_val < bucket.low:
                gap = round(bucket.low - res_val, 1) if rises else round(res_val - bucket.low, 1)
            elif bucket.high is not None and res_val > bucket.high:
                gap = round(bucket.high - res_val, 1) if rises else round(res_val - bucket.high, 1)
        elif extreme is not None and threshold is not None:
            gap = round(threshold - extreme, 1) if yes_if_above else round(extreme - threshold, 1)
        p_win = sig.get("p_win")
        price = prices.get(market_id) or {}
        yes_price = price.get("yes")
        edge = None
        side = sig.get("recommended_side")
        if isinstance(p_win, (int, float)) and isinstance(yes_price, (int, float)):
            if side == "YES":
                edge = round(float(p_win) - float(yes_price), 3)
            elif side == "NO":
                edge = round((1.0 - float(p_win)) - (1.0 - float(yes_price)), 3)
        deadline = str(cfg.get("deadline_local_hhmm") or "23:59")
        try:
            hh, mm = (int(x) for x in deadline.split(":")[:2])
            mins_left = (hh - local_now.hour) * 60 + (mm - local_now.minute)
        except ValueError:
            mins_left = None
        rows.append(
            {
                "market_id": market_id,
                "name": cfg.get("name") or market_id[:12],
                "icao": icao,
                "variable": var,
                "threshold": threshold,
                "unit": unit,
                "yes_if_above": yes_if_above,
                "target_date": cfg.get("target_date") or cfg.get("target_date_start"),
                "timezone": tz,
                "local_time": local_now.strftime("%H:%M"),
                "deadline_local": deadline,
                "minutes_to_deadline": mins_left,
                "day_extreme": extreme,
                "day_extreme_c": extreme_c,
                "resolution_value": res_val,
                "lead_resolution_value": lead_val,
                "bucket": {"low": bucket.low, "high": bucket.high} if bucket is not None else None,
                "gap": gap,
                "obs_count": st.get("obs_count") if isinstance(st, dict) else None,
                "signal": {
                    "p_win": p_win,
                    "binary_state": sig.get("binary_state"),
                    "recommended_side": side,
                    "current_obs_temp": sig.get("current_obs_temp"),
                    "instant_obs_temp": sig.get("instant_obs_temp"),
                    "resolution_model": sig.get("resolution_model") or sig.get("observation_model"),
                    "reason": sig.get("decision_reason"),
                    "rt_day_guard": sig.get("rt_day_guard"),
                    "obs_time_local": sig.get("obs_time_local") or sig.get("observation_local"),
                },
                "price": price or None,
                "edge": edge,
            }
        )
    rows.sort(key=lambda r: (r["signal"]["binary_state"] != "open", r["icao"], r["name"]))
    return rows


# -- Polymarket prices (Gamma) ----------------------------------------------------------

class GammaPrices:
    """
    YES prices per condition id from gamma-api.polymarket.com, refreshed in a
    background thread so a page request never waits on Polymarket: ``get`` returns
    whatever is cached and schedules a refresh when the cache is older than ``ttl``.
    """

    def __init__(self, ttl_sec: float = 60.0, base_url: Optional[str] = None, workers: int = 8) -> None:
        self.ttl = ttl_sec
        self.base_url = (base_url or os.getenv("GAMMA_API_URL") or "https://gamma-api.polymarket.com").rstrip("/")
        self.enabled = (os.getenv("UI_POLYMARKET_PRICES") or "1").strip() != "0"
        self.workers = max(1, workers)
        self._cache: dict[str, dict[str, Any]] = {}
        self._at = 0.0
        self._lock = threading.Lock()
        self._refreshing = False
        self.last_error = ""

    def get(self, market_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not self.enabled or not market_ids:
            return {}
        with self._lock:
            stale = time.monotonic() - self._at > self.ttl or any(m not in self._cache for m in market_ids)
            if stale and not self._refreshing:
                self._refreshing = True
                threading.Thread(target=self._refresh, args=(list(market_ids),), daemon=True).start()
            return {m: self._cache[m] for m in market_ids if m in self._cache}

    def fetch_now(self, market_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Synchronous fetch (tests, scripts)."""
        self._refresh(list(market_ids))
        with self._lock:
            return {m: self._cache[m] for m in market_ids if m in self._cache}

    def _refresh(self, market_ids: list[str]) -> None:
        try:
            fetched = self._fetch(market_ids)
            with self._lock:
                self._cache.update(fetched)
                self._at = time.monotonic()
                self.last_error = ""
        except Exception as e:  # noqa: BLE001 - prices are decoration, never block the page
            with self._lock:
                self.last_error = str(e)
                self._at = time.monotonic()
            LOG.warning("gamma prices unavailable: %s", e)
        finally:
            with self._lock:
                self._refreshing = False

    def _fetch(self, market_ids: list[str]) -> dict[str, dict[str, Any]]:
        import httpx
        from concurrent.futures import ThreadPoolExecutor

        ids = [m for m in market_ids if m.startswith("0x")]
        chunks = [ids[i : i + 20] for i in range(0, len(ids), 20)]
        out: dict[str, dict[str, Any]] = {}

        def one(chunk: list[str]) -> dict[str, dict[str, Any]]:
            res: dict[str, dict[str, Any]] = {}
            for attempt in range(2):
                try:
                    r = httpx.get(f"{self.base_url}/markets", params=[("condition_ids", c) for c in chunk], timeout=20.0)
                    r.raise_for_status()
                    for m in r.json() or []:
                        cid = str(m.get("conditionId") or "")
                        if cid:
                            res[cid] = _price_row(m)
                    return res
                except Exception as e:  # noqa: BLE001
                    if attempt == 1:
                        LOG.debug("gamma chunk failed: %s", e)
            return res

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for res in pool.map(one, chunks):
                out.update(res)
        return out


def _price_row(m: dict[str, Any]) -> dict[str, Any]:
    prices = m.get("outcomePrices")
    try:
        plist = json.loads(prices) if isinstance(prices, str) else (prices or [])
        yes = float(plist[0]) if plist else None
    except (ValueError, TypeError, IndexError):
        yes = None
    return {
        "yes": yes,
        "best_bid": _f(m.get("bestBid")),
        "best_ask": _f(m.get("bestAsk")),
        "spread": _f(m.get("spread")),
        "liquidity": _f(m.get("liquidityNum") or m.get("liquidity")),
        "volume_24h": _f(m.get("volume24hr")),
        "question": m.get("question"),
        "slug": m.get("slug"),
        "closed": bool(m.get("closed")),
    }


def _is_near_term(cfg: Any, now: datetime) -> bool:
    """Markets whose target date is today or tomorrow in the station's local zone."""
    if not isinstance(cfg, dict):
        return False
    try:
        target = date.fromisoformat(str(cfg.get("target_date") or cfg.get("target_date_start") or ""))
    except ValueError:
        return True  # manual entries without a parseable date keep their prices
    today = _local_now(str(cfg.get("timezone") or "UTC"), now).date()
    return today <= target <= today + timedelta(days=1)


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# -- top-level assembly ------------------------------------------------------------------

class Dashboard:
    def __init__(self, *, snapshot_file: Optional[Path] = None, prices: Optional[GammaPrices] = None) -> None:
        self.snapshot_file = snapshot_file or snapshot_path()
        self.prices = prices or GammaPrices()

    def watched_files(self) -> list[Path]:
        from ui.history import ALERTS_FILE, EVENTS_FILE, cycle_log_path, portfolio_path

        sig, meta = signals_paths()
        return [self.snapshot_file, sig, meta, mapping_path(), portfolio_path(), cycle_log_path(), ALERTS_FILE, EVENTS_FILE]

    def history(self, now: Optional[datetime] = None) -> dict[str, Any]:
        from ui import history as h

        now = now or datetime.now(timezone.utc)
        mapping = _load_json(mapping_path()) or {}
        names = {k: str(v.get("name") or "") for k, v in mapping.items() if isinstance(v, dict)} if isinstance(mapping, dict) else {}
        return h.history_view(
            positions=h.read_positions(h.portfolio_path()),
            names=names,
            cycles=h.parse_cycle_summaries(h._tail_lines(h.cycle_log_path(), 4000)),
            alerts=h.read_jsonl(h.ALERTS_FILE),
            events=h.read_jsonl(h.EVENTS_FILE),
            now=now,
        )

    def stream(self, now: Optional[datetime] = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        return stream_view(read_snapshot(self.snapshot_file), served_stations(), now)

    def stations(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        return station_board(read_snapshot(self.snapshot_file), served_stations(), now)

    def markets(self, now: Optional[datetime] = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        mapping = _load_json(mapping_path()) or {}
        if not isinstance(mapping, dict):
            mapping = {}
        sig_path, meta_path = signals_paths()
        signals, meta = load_signals(sig_path, meta_path)
        ids = [k for k in mapping if isinstance(k, str) and not k.startswith("_") and _is_near_term(mapping[k], now)]
        prices = self.prices.get(ids)
        rows = market_rows(mapping, signals, read_snapshot(self.snapshot_file), prices, now)
        gen = meta.get("generated_at_utc") or meta.get("last_run_utc")
        gen_dt = parse_utc(gen)
        return {
            "generated_at_utc": gen,
            "signals_age_sec": round((now - gen_dt).total_seconds(), 1) if gen_dt else None,
            "status": meta.get("status"),
            "health": meta.get("signals_health"),
            "pipeline": pipeline(),
            "observation_source": (os.getenv("METAR_SOURCE") or "awc"),
            "rows": rows,
        }

    def overview(self, now: Optional[datetime] = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        return {
            "now_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stream": self.stream(now),
            "stations": self.stations(now),
            "markets": self.markets(now),
            "history": self.history(now),
        }
