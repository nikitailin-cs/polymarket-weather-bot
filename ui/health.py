"""
Connection health for the dashboard topbar.

- METAR.ws: derived from the daemon snapshot (connected flag, plan, channels,
  snapshot age, last message, reconnects, forecast records).
- Polymarket Gamma and CLOB: probed by a background thread (``GET /markets?limit=1``
  and ``GET /time``) every ``UI_HEALTH_INTERVAL_SEC`` (60) with latency and last error.
- Trading cycle: last cycle summary and its age (from the history parser).
- Settings view: effective configuration with secrets masked.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.metar_observation import parse_utc

LOG = logging.getLogger("ui.health")
_ROOT = Path(__file__).resolve().parent.parent

SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|PRIVATE|PASSWORD|PKEY|CHAT_ID|ADDRESS|RPC_URL)", re.I)


def mask(name: str, value: Any) -> Any:
    if value is None or value == "":
        return value
    s = str(value)
    if SECRET_RE.search(name or ""):
        return s[:4] + "…" + s[-2:] if len(s) > 10 else "•••"
    return s


class ConnectionHealth:
    """Background probes for Polymarket endpoints; call :meth:`start` once in the server."""

    def __init__(self, *, interval_sec: Optional[float] = None, gamma_url: Optional[str] = None, clob_url: Optional[str] = None) -> None:
        raw = os.getenv("UI_HEALTH_INTERVAL_SEC") or ""
        self.interval = interval_sec if interval_sec is not None else (float(raw) if raw.strip() else 60.0)
        self.gamma_url = (gamma_url or os.getenv("GAMMA_API_URL") or "https://gamma-api.polymarket.com").rstrip("/")
        self.clob_url = (clob_url or os.getenv("CLOB_API_URL") or "https://clob.polymarket.com").rstrip("/")
        self.enabled = (os.getenv("UI_POLYMARKET_PRICES") or "1").strip() != "0"
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, Any]] = {
            "gamma": {"status": "unknown", "latency_ms": None, "checked_at_utc": None, "error": ""},
            "clob": {"status": "unknown", "latency_ms": None, "checked_at_utc": None, "error": "", "server_time": None},
        }
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ui-health")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.probe_once()
            self._stop.wait(self.interval)

    def probe_once(self) -> None:
        import httpx

        for name, url, parse in (
            ("gamma", f"{self.gamma_url}/markets?limit=1", lambda r: None),
            ("clob", f"{self.clob_url}/time", lambda r: r.text.strip()[:40]),
        ):
            t0 = time.monotonic()
            row: dict[str, Any] = {"checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            try:
                r = httpx.get(url, timeout=10.0)
                r.raise_for_status()
                row.update({"status": "ok", "latency_ms": round((time.monotonic() - t0) * 1000), "error": ""})
                if name == "clob":
                    row["server_time"] = parse(r)
            except Exception as e:  # noqa: BLE001
                row.update({"status": "down", "latency_ms": None, "error": f"{type(e).__name__}: {str(e)[:120]}"})
            with self._lock:
                self._state[name].update(row)

    def view(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}


def metar_ws_view(snapshot: Optional[dict[str, Any]], now: datetime) -> dict[str, Any]:
    if snapshot is None:
        return {"status": "no_snapshot", "connected": False}
    client = snapshot.get("client") or {}
    gen = parse_utc(snapshot.get("generated_at_utc"))
    last_msg = parse_utc(client.get("last_message_at_utc"))
    age = (now - gen).total_seconds() if gen else None
    connected = bool(snapshot.get("connected"))
    quiet_after = float(os.getenv("UI_METAR_QUIET_SEC") or 1800)  # 30 min without a snapshot write -> yellow
    had_session = bool(client.get("messages")) and not (client.get("last_error") or "")
    if age is not None and age > quiet_after:
        status = "stale"  # daemon has not written for a long time
    elif connected or had_session:
        status = "ok"  # connection established (or cleanly stopped after a working session): green
    else:
        status = "disconnected"
    wanted = client.get("wanted_channels") or []
    return {
        "status": status,
        "connected": connected,
        "plan": client.get("plan"),
        "limits": client.get("limits"),
        "channels_subscribed": len(client.get("subscribed") or []),
        "channels_wanted": len(wanted),
        "stations": len(snapshot.get("stations") or {}),
        "snapshot_age_sec": round(age, 1) if age is not None else None,
        "last_message_age_sec": round((now - last_msg).total_seconds(), 1) if last_msg else None,
        "reconnects": client.get("reconnects"),
        "messages": client.get("messages"),
        "forecast_records": client.get("forecast_records"),
        "station_days_tracked": client.get("station_days_tracked"),
        "last_error": client.get("last_error") or "",
        "failed_channels": len(client.get("failed_channels") or {}),
    }


def settings_view(*, paths: dict[str, str], mapping_stats: dict[str, Any], mode: str) -> dict[str, Any]:
    """Effective configuration for the Settings page (read-only, secrets masked)."""
    keys = [
        "DRY_RUN", "PAPER_TRADING", "METAR_SOURCE", "UPDATE_BOT_RT_PIPELINE",
        "METAR_WS_API_KEY", "METAR_WS_URL", "METAR_WS_FORECASTS", "METAR_WS_CHANNEL_FORMAT", "METAR_WS_TRIGGER_CMD",
        "METAR_WS_TRIGGER_DEBOUNCE_SEC", "METAR_WS_SNAPSHOT_MAX_AGE_SEC", "METAR_WS_FALLBACK_AWC",
        "FINANCE_RT_MAX_BET_USDC", "FINANCE_RT_MAX_BEST_ASK_USDC", "FINANCE_RT_MIN_ORDER_NOTIONAL_USDC",
        "FINANCE_RT_MAX_TARGETS_PER_CYCLE", "FINANCE_RT_PROB_TRADING", "FINANCE_RT_PROB_MIN_EDGE",
        "FINANCE_RT_MAX_PROB_TARGETS", "FINANCE_RT_L2_WORKERS", "RUN_CYCLE_STEP_TIMEOUT_SEC",
        "FORECAST_SIGMA_HIRES_C", "FORECAST_SIGMA_BASIC_C", "FORECAST_SIGMA_LATE_C", "FORECAST_PEAK_PASSED_HOURS",
        "REALTIME_SIGNALS_MAX_AGE_SEC", "POLYGON_RPC_URL", "PRIVATE_KEY", "POLYMARKET_PROXY_ADDRESS",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "UI_HOST", "UI_PORT", "UI_TOKEN", "UI_POLYMARKET_PRICES",
    ]
    env_file = _ROOT / ".env"
    file_values: dict[str, str] = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            file_values[k.strip()] = v.strip()
    rows = []
    for k in keys:
        in_env = os.getenv(k)
        in_file = file_values.get(k)
        value = in_env if in_env is not None else in_file
        rows.append({
            "name": k,
            "value": mask(k, value),
            "set": value is not None and value != "",
            "source": "env" if in_env is not None else ("file" if in_file is not None else "default"),
            "secret": bool(SECRET_RE.search(k)),
        })
    return {
        "mode": mode,
        "env_file": str(env_file) if env_file.is_file() else None,
        "values": rows,
        "paths": paths,
        "mapping": mapping_stats,
        "python": os.sys.version.split()[0],
        "cwd": str(_ROOT),
    }
