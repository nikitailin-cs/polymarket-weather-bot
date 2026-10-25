"""
History for the dashboard: positions and PnL from ``portfolio_rt.sqlite``, cycle
summaries from ``logs/update_bot_rt_cycle.log``, persisted alerts
(``logs/alerts.jsonl``) and daemon events (``logs/metar_ws_events.jsonl``).
Read-only: the SQLite file is opened in ``mode=ro``.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from core.metar_observation import parse_utc

LOG = logging.getLogger("ui.history")
_ROOT = Path(__file__).resolve().parent.parent

ALERTS_FILE = _ROOT / "logs" / "alerts.jsonl"
EVENTS_FILE = _ROOT / "logs" / "metar_ws_events.jsonl"


def trading_mode() -> str:
    """``live`` (real orders), ``paper`` (DRY_RUN + paper fills) or ``dry`` (DRY_RUN, log only)."""
    from execution.paper_broker import paper_enabled

    v = (os.getenv("FINANCE_RT_DRY_RUN") or os.getenv("FINANCE_ENGINE_RT_DRY_RUN") or os.getenv("DRY_RUN") or "1").strip().lower()
    dry = v not in ("0", "false", "no", "off")
    if not dry:
        return "live"
    return "paper" if paper_enabled() else "dry"


def portfolio_path() -> Path:
    if trading_mode() == "paper":
        from execution.paper_broker import paper_portfolio_path

        return paper_portfolio_path()
    if (os.getenv("UPDATE_BOT_RT_PIPELINE") or "bot2").strip().lower() == "bot1":
        from core.bot1_realtime_data_paths import portfolio_rt_sqlite_path

        return portfolio_rt_sqlite_path()
    from core.bot2_data_paths import get_rt_paths

    return get_rt_paths()["db"]


def cycle_log_path() -> Path:
    raw = (os.getenv("UPDATE_BOT_RT_CYCLE_LOG") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else _ROOT / p
    return _ROOT / "logs" / "update_bot_rt_cycle.log"


# -- positions -----------------------------------------------------------------------

def read_positions(path: Path, limit: int = 500) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as conn:
            conn.row_factory = sqlite3.Row
            cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()}
            if not cols:
                return []
            rows = conn.execute(
                "SELECT rowid AS id, * FROM positions ORDER BY timestamp DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
    except sqlite3.Error as e:
        LOG.warning("portfolio unreadable (%s): %s", path, e)
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        price = _f(d.get("price"))
        size = _f(d.get("size"))
        out.append(
            {
                "id": d.get("id"),
                "market_id": str(d.get("market_id") or ""),
                "side": str(d.get("side") or "").upper(),
                "price": price,
                "size": size,
                "notional": round(price * size, 4) if price is not None and size is not None else None,
                "max_payout": round(size, 4) if size is not None else None,
                "status": str(d.get("status") or "").upper(),
                "opened_at": d.get("timestamp"),
                "settled_at": d.get("settled_at"),
                "pnl": _f(d.get("pnl")),
            }
        )
    return out


def positions_summary(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    open_notional = 0.0
    realized = 0.0
    realized_today = 0.0
    wins = losses = 0
    today = now.date()
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        if r["status"] == "OPEN" and r["notional"]:
            open_notional += r["notional"]
        if r["pnl"] is not None and r["status"] == "SETTLED":
            realized += r["pnl"]
            if r["pnl"] > 0:
                wins += 1
            elif r["pnl"] < 0:
                losses += 1
            st = parse_utc(r.get("settled_at"))
            if st and st.date() == today:
                realized_today += r["pnl"]
    return {
        "count": len(rows),
        "by_status": by_status,
        "open_notional_usdc": round(open_notional, 2),
        "realized_pnl_usdc": round(realized, 2),
        "realized_today_usdc": round(realized_today, 2),
        "wins": wins,
        "losses": losses,
    }


# -- cycle log -----------------------------------------------------------------------

def _tail_lines(path: Path, max_lines: int, max_bytes: int = 2_000_000) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop the partial line
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return list(deque(data.splitlines(), maxlen=max_lines))


def parse_cycle_summaries(lines: Iterable[str], limit: int = 100) -> list[dict[str, Any]]:
    """``cycle_summary {json}`` lines (and the bare JSON echo) from update_bot_rt.sh, newest first."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for line in lines:
        s = line.strip()
        idx = s.find("cycle_summary ")
        if idx >= 0:
            s = s[idx + len("cycle_summary ") :]
        if not s.startswith("{"):
            continue
        try:
            d = json.loads(s)
        except ValueError:
            continue
        if not isinstance(d, dict) or "ts" not in d or "step_exit_codes" not in d:
            continue
        key = f"{d.get('ts')}|{d.get('pipeline')}"
        if key in seen:
            continue
        seen.add(key)
        steps = d.get("step_exit_codes") or {}
        out.append(
            {
                "ts": d.get("ts"),
                "pipeline": d.get("pipeline"),
                "metar": steps.get("metar"),
                "finance": steps.get("finance_rt"),
                "finance_skipped": bool(steps.get("finance_rt_skipped")),
                "settlement": steps.get("settlement_chain"),
                "exit_code": d.get("aggregated_exit_code"),
                "ok": d.get("aggregated_exit_code") == 0,
            }
        )
    out.sort(key=lambda r: str(r["ts"]), reverse=True)
    return out[:limit]


# -- jsonl logs (alerts, daemon events) ------------------------------------------------

def read_jsonl(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in _tail_lines(path, limit * 2):
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            d = json.loads(s)
        except ValueError:
            continue
        if isinstance(d, dict):
            rows.append(d)
    rows.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
    return rows[:limit]


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    """Shared writer used by the alert sender and the daemon (best effort)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **row}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        LOG.debug("jsonl append failed (%s): %s", path, e)


# -- assembly ------------------------------------------------------------------------

def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def history_view(
    *,
    positions: list[dict[str, Any]],
    names: dict[str, str],
    cycles: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    events: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    for p in positions:
        p["name"] = names.get(p["market_id"], "")
    last_cycle = cycles[0] if cycles else None
    last_ok = next((c for c in cycles if c["ok"]), None)
    last_ok_dt = parse_utc(last_ok["ts"]) if last_ok else None
    return {
        "mode": trading_mode(),
        "portfolio": str(portfolio_path()),
        "summary": positions_summary(positions, now),
        "positions": positions,
        "cycles": cycles,
        "last_cycle": last_cycle,
        "minutes_since_ok_cycle": round((now - last_ok_dt).total_seconds() / 60, 1) if last_ok_dt else None,
        "alerts": alerts,
        "events": events,
    }
