#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot Telegram interactif (long polling) — rapports live, positions, derniers cycles.

Prérequis : mêmes variables que les alertes (``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID``).
Seules les commandes envoyées depuis ``TELEGRAM_CHAT_ID`` sont traitées (sécurité).

Usage serveur (systemd ou tmux) :
  cd ~/polymarket-weather-bot && source .venv/bin/activate
  python monitoring/telegram_command_bot.py

Test ponctuel (un seul poll) :
  python monitoring/telegram_command_bot.py --once
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env", override=False)
except ImportError:
    pass

TG_MAX_MSG = 3900


def _esc(s: str, mx: int = 800) -> str:
    t = (s or "")[:mx]
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _load_settings() -> tuple[str, str]:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat


def _authorized(chat_id: Any, allowed: str) -> bool:
    if not allowed:
        return False
    return str(chat_id).strip() == str(allowed).strip()


def _api_get(token: str, method: str, **params: Any) -> dict[str, Any]:
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"https://api.telegram.org/bot{token}/{method}?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-telegram-cmd/1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _api_post_json(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "polymarket-telegram-cmd/1"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def send_message_html(token: str, chat_id: str, text: str) -> None:
    for chunk in _split_chunks(text):
        _api_post_json(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )


def _split_chunks(text: str) -> list[str]:
    if len(text) <= TG_MAX_MSG:
        return [text]
    out: list[str] = []
    buf = ""
    for line in text.split("\n"):
        add = (buf + "\n" if buf else "") + line
        if len(add) > TG_MAX_MSG:
            if buf:
                out.append(buf)
            buf = line[:TG_MAX_MSG] if len(line) > TG_MAX_MSG else line
        else:
            buf = add
    if buf:
        out.append(buf)
    return out if out else [text[:TG_MAX_MSG]]


def _systemd_is_active(unit: str) -> str:
    try:
        p = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return (p.stdout or "").strip() or "unknown"
    except FileNotFoundError:
        return "n/a (pas systemctl)"
    except Exception as exc:
        return f"err: {exc}"


def _tail_file(path: Path, n: int = 25) -> str:
    if not path.is_file():
        return f"(absent) {path}"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError as exc:
        return f"(lecture impossible: {exc})"


def _json_snippet(path: Path, max_chars: int = 2800) -> str:
    if not path.is_file():
        return f"(absent) {path}"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
        if len(pretty) > max_chars:
            return pretty[: max_chars - 40] + "\n… (tronqué)"
        return pretty
    except Exception as exc:
        return f"(parse error: {exc})"


def _bot1_trades(limit: int = 12) -> str:
    from core.bot1_data_paths import portfolio_sqlite_path

    db = portfolio_sqlite_path()
    if not db.is_file():
        return f"SQLite absent: {db}"
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            """
            SELECT traded_at_utc, market_id, recommended_side, effective_p_win, best_ask, ev
            FROM executed_trades
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
    except sqlite3.Error as exc:
        return f"SQLite: {exc}"
    finally:
        conn.close()
    if not rows:
        return "(aucune ligne executed_trades)"
    lines = ["<b>Bot1 — derniers trades</b> (portfolio batch/RT selon env)"]
    for r in rows:
        ts, mid, side, pw, ask, ev = r[:6]
        mshort = str(mid)[:14] + "…" if len(str(mid)) > 16 else str(mid)
        lines.append(
            f"• <code>{_esc(str(ts), 24)}</code> {mshort} "
            f"<b>{_esc(str(side), 4)}</b> p={float(pw):.3f} ask={float(ask):.3f} "
            f"ev={float(ev):.3f}"
        )
    return "\n".join(lines)


def _bot2_positions(limit: int = 15) -> str:
    from core.bot2_data_paths import BOT2_PORTFOLIO_SQLITE_PATH

    db = BOT2_PORTFOLIO_SQLITE_PATH
    if not db.is_file():
        return f"SQLite bot2 absent: {db}"
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            """
            SELECT market_id, side, price, size, timestamp, status, IFNULL(pnl, ''), IFNULL(settled_at, '')
            FROM positions
            ORDER BY rowid DESC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
    except sqlite3.Error as exc:
        return f"SQLite bot2: {exc}"
    finally:
        conn.close()
    if not rows:
        return "(aucune ligne positions bot2)"
    lines = ["<b>Bot2 RT — positions (dernières lignes)</b>"]
    for r in rows:
        mid, side, price, size, ts, status, pnl, sat = r
        mshort = str(mid)[:16] + "…" if len(str(mid)) > 18 else str(mid)
        lines.append(
            f"• {mshort} <b>{_esc(str(side), 4)}</b> @ {float(price):.4f} size={float(size):.4f} "
            f"<i>{_esc(str(status), 14)}</i> {_esc(str(ts), 24)}"
        )
    return "\n".join(lines)


def _rt_health_text() -> str:
    try:
        from core.bot2_data_paths import REALTIME_SIGNALS_MAX_AGE_SEC, get_rt_paths
        import time as tmod

        paths = get_rt_paths()
        meta = paths["meta"]
        if not meta.is_file():
            return f"meta absent: {meta}"
        raw = json.loads(meta.read_text(encoding="utf-8"))
        ts = int(raw.get("generated_at_unix") or raw.get("timestamp") or 0)
        age = int(tmod.time()) - ts
        health = raw.get("signals_health") or {}
        hstat = str(health.get("health_status", "?"))
        return (
            f"<b>Bot2 RT — santé</b>\n"
            f"meta: <code>{_esc(str(meta), 120)}</code>\n"
            f"âge: <b>{age}s</b> (max {REALTIME_SIGNALS_MAX_AGE_SEC}s)\n"
            f"health: <b>{_esc(hstat, 16)}</b>\n"
            f"marchés traités: {raw.get('markets_processed', '?')}"
        )
    except Exception as exc:
        return f"rt_health err: {exc}"


def _markets_scan() -> str:
    old_pages = os.environ.get("GAMMA_MAX_PAGES")
    os.environ["GAMMA_MAX_PAGES"] = "2"
    buf = io.StringIO()
    try:
        import contextlib

        with contextlib.redirect_stderr(buf):
            from scanners.market_scanner import scan_weather_markets

            mkts = scan_weather_markets(limit=80)
    except Exception as exc:
        return f"scan err: {exc}"
    finally:
        if old_pages is None:
            os.environ.pop("GAMMA_MAX_PAGES", None)
        else:
            os.environ["GAMMA_MAX_PAGES"] = old_pages

    err_log = buf.getvalue().strip()
    lines = [
        f"<b>Marchés météo (scan)</b> — {len(mkts)} marché(s) filtré(s)",
        f"<pre>{_esc(err_log[-500:], 500)}</pre>" if err_log else "",
    ]
    for m in mkts[:18]:
        q = getattr(m, "question", None) or str(m)
        cid = getattr(m, "condition_id", "") or ""
        lines.append(f"• {_esc(str(q)[:140], 140)}")
        if cid:
            lines.append(f"  <code>{_esc(str(cid)[:66], 66)}</code>")
    if len(mkts) > 18:
        lines.append(f"… +{len(mkts) - 18} autre(s)")
    return "\n".join(lines)


def _normalize_cmd(text: str) -> str:
    t = (text or "").strip()
    if "@" in t:
        t = t.split("@", 1)[0].strip()
    return t.split()[0].lower() if t else ""


def _help_text() -> str:
    return (
        "<b>Commandes Polymarket bot</b>\n"
        "/help — cette aide\n"
        "/ping — test\n"
        "/health — systemd + meta bot1/bot2 + résumé RT\n"
        "/lastcycle — dernières lignes update_bot_cycle.log\n"
        "/daemon — fin de bot1_adaptive_daemon.log\n"
        "/bot1meta — contenu trading_signals_meta.json\n"
        "/bot2meta — contenu realtime_signals_meta.json\n"
        "/positions — derniers trades portfolio bot1 (SQLite)\n"
        "/positions2 — dernières lignes positions bot2 RT\n"
        "/rt — santé signaux temps réel (meta + âge)\n"
        "/markets — scan Gamma météo (peut prendre ~10–30s)\n"
        "/status — alias /health"
    )


def handle_command(token: str, chat_id: str, text: str) -> None:
    cmd = _normalize_cmd(text)
    if cmd in ("/start", "/help", ""):
        send_message_html(token, chat_id, _help_text())
        return
    if cmd == "/ping":
        send_message_html(token, chat_id, "pong ✅")
        return

    repo = _ROOT
    log_cycle = repo / "logs" / "update_bot_cycle.log"
    log_daemon = repo / "logs" / "bot1_adaptive_daemon.log"

    if cmd in ("/health", "/status"):
        from core.bot1_data_paths import trading_signals_meta_path

        u1 = _systemd_is_active("polymarket-bot1-adaptive.service")
        ut = _systemd_is_active("polymarket-bot-rt.timer")
        parts = [
            "<b>État services</b>",
            f"polymarket-bot1-adaptive: <b>{_esc(u1, 32)}</b>",
            f"polymarket-bot-rt.timer: <b>{_esc(ut, 32)}</b>",
            "",
            _rt_health_text(),
            "",
            "<b>Meta NOAA (extrait)</b>",
            f"<pre>{_esc(_json_snippet(trading_signals_meta_path(), 1500), 3500)}</pre>",
        ]
        send_message_html(token, chat_id, "\n".join(parts))
        return

    if cmd == "/lastcycle":
        send_message_html(
            token,
            chat_id,
            f"<b>Dernier cycle (update_bot_cycle)</b>\n<pre>{_esc(_tail_file(log_cycle, 12), 3500)}</pre>",
        )
        return

    if cmd == "/daemon":
        send_message_html(
            token,
            chat_id,
            f"<b>bot1_adaptive_daemon</b>\n<pre>{_esc(_tail_file(log_daemon, 22), 3500)}</pre>",
        )
        return

    if cmd == "/bot1meta":
        from core.bot1_data_paths import trading_signals_meta_path

        send_message_html(
            token,
            chat_id,
            f"<b>trading_signals_meta</b>\n<pre>{_esc(_json_snippet(trading_signals_meta_path()), 3500)}</pre>",
        )
        return

    if cmd == "/bot2meta":
        from core.bot2_data_paths import REALTIME_SIGNALS_META_FILE

        send_message_html(
            token,
            chat_id,
            f"<b>realtime_signals_meta</b>\n<pre>{_esc(_json_snippet(REALTIME_SIGNALS_META_FILE), 3500)}</pre>",
        )
        return

    if cmd == "/positions":
        send_message_html(token, chat_id, _bot1_trades(14))
        return

    if cmd in ("/positions2", "/pos2"):
        send_message_html(token, chat_id, _bot2_positions(16))
        return

    if cmd in ("/rt", "/rthealth"):
        send_message_html(token, chat_id, _rt_health_text())
        return

    if cmd == "/markets":
        send_message_html(token, chat_id, "Scan en cours (Gamma)… ⏳")
        send_message_html(token, chat_id, _markets_scan())
        return

    send_message_html(token, chat_id, f"Commande inconnue. /help — liste des commandes.")


def run_loop(*, once: bool = False) -> None:
    token, allowed_chat = _load_settings()
    if not token or not allowed_chat:
        print("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID requis dans .env", file=sys.stderr)
        sys.exit(1)

    offset: Optional[int] = None
    print("telegram_command_bot: polling… (Ctrl+C pour arrêter)", flush=True)
    while True:
        try:
            params: dict[str, Any] = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            data = _api_get(token, "getUpdates", **params)
            if not data.get("ok"):
                print("getUpdates not ok:", data, file=sys.stderr)
                time.sleep(2)
                continue
            updates = data.get("result") or []
            for u in updates:
                offset = int(u.get("update_id", 0)) + 1
                msg = u.get("message") or {}
                chat = msg.get("chat") or {}
                cid = chat.get("id")
                text = (msg.get("text") or "").strip()
                if not text or not text.startswith("/"):
                    continue
                if not _authorized(cid, allowed_chat):
                    print(f"Ignoré chat_id={cid}", flush=True)
                    continue
                try:
                    handle_command(token, str(allowed_chat), text)
                except Exception as exc:
                    try:
                        send_message_html(
                            token,
                            str(allowed_chat),
                            f"Erreur commande: {_esc(str(exc), 400)}",
                        )
                    except Exception:
                        pass
                    print(exc, file=sys.stderr)
            if once and offset is not None:
                return
        except KeyboardInterrupt:
            print("Arrêt.", flush=True)
            return
        except Exception as exc:
            print("poll:", exc, file=sys.stderr)
            time.sleep(3)


def main() -> int:
    ap = argparse.ArgumentParser(description="Telegram: commandes rapport live")
    ap.add_argument("--once", action="store_true", help="Un seul tour de getUpdates puis exit")
    args = ap.parse_args()
    run_loop(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
