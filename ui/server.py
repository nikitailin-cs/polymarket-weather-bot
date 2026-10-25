#!/usr/bin/env python3
"""
Read-only dashboard server (standard library only).

    python -m ui.server                 # http://127.0.0.1:8765
    UI_PORT=9000 UI_TOKEN=secret python -m ui.server

Endpoints:

- ``GET /``                  the single-page dashboard
- ``GET /api/overview``      stream + stations + markets in one call
- ``GET /api/stream``        connection state and per-station freshness
- ``GET /api/stations``      daily extremes board
- ``GET /api/markets``       mapping x signals x daily extreme x Polymarket price
- ``GET /api/history``       positions + PnL (portfolio_rt.sqlite), cycle summaries, alerts, daemon events
- ``GET /api/connections``   topbar health: METAR.ws, Gamma, CLOB, prices cache, last cycle
- ``GET /api/settings``      effective configuration, secrets masked (read-only)
- ``GET /api/events``        Server-Sent Events: ``change`` whenever a watched file changes

Environment: ``UI_HOST`` (127.0.0.1), ``UI_PORT`` (8765), ``UI_TOKEN`` (optional bearer /
``?token=`` guard; required when binding to anything but loopback), ``UI_POLYMARKET_PRICES``
(``0`` disables Gamma lookups).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ui.data import Dashboard  # noqa: E402

LOG = logging.getLogger("ui.server")
STATIC = Path(__file__).resolve().parent / "static"


def _mtimes(paths: list[Path]) -> tuple[float, ...]:
    out = []
    for p in paths:
        try:
            out.append(p.stat().st_mtime)
        except OSError:
            out.append(0.0)
    return tuple(out)


class Handler(BaseHTTPRequestHandler):
    dashboard: Dashboard
    token: str = ""
    server_version = "weatherbot-ui/1.0"

    # -- helpers -----------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        if not self.token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:].strip() == self.token:
            return True
        return query.get("token", [""])[0] == self.token

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routes ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = url.path
        if path.startswith("/api/") and not self._authorized(query):
            self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            if path in ("/", "/index.html"):
                self._send_file(STATIC / "index.html", "text/html; charset=utf-8")
            elif path == "/api/overview":
                self._send_json(self.dashboard.overview())
            elif path == "/api/stream":
                self._send_json(self.dashboard.stream())
            elif path == "/api/stations":
                self._send_json(self.dashboard.stations())
            elif path == "/api/markets":
                self._send_json(self.dashboard.markets())
            elif path == "/api/history":
                self._send_json(self.dashboard.history())
            elif path == "/api/connections":
                self._send_json(self.dashboard.connections())
            elif path == "/api/settings":
                self._send_json(self.dashboard.settings())
            elif path == "/api/events":
                self._sse()
            elif path == "/healthz":
                self._send_json({"ok": True})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            LOG.exception("request failed: %s", e)
            try:
                self._send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:  # noqa: BLE001
                pass

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        watched = self.dashboard.watched_files()
        last = _mtimes(watched)
        self.wfile.write(b"event: hello\ndata: {}\n\n")
        self.wfile.flush()
        beat = time.monotonic()
        while True:
            time.sleep(1.0)
            cur = _mtimes(watched)
            if cur != last:
                last = cur
                self.wfile.write(b"event: change\ndata: {}\n\n")
                self.wfile.flush()
                beat = time.monotonic()
            elif time.monotonic() - beat > 25:
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                beat = time.monotonic()


def serve(host: str, port: int, token: str = "", dashboard: Dashboard | None = None) -> ThreadingHTTPServer:
    Handler.dashboard = dashboard or Dashboard()
    Handler.token = token
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, (os.getenv("LOG_LEVEL") or "INFO").upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    try:
        from dotenv import load_dotenv

        load_dotenv(_ROOT / ".env")
    except Exception:  # noqa: BLE001
        pass
    host = (os.getenv("UI_HOST") or "127.0.0.1").strip()
    port = int(os.getenv("UI_PORT") or "8765")
    token = (os.getenv("UI_TOKEN") or "").strip()
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        LOG.error("refusing to bind %s without UI_TOKEN (positions and wallet data are exposed)", host)
        return 2
    httpd = serve(host, port, token)
    Handler.dashboard.health.start()
    LOG.info("dashboard on http://%s:%s/ (pipeline=%s)", host, port, os.getenv("UPDATE_BOT_RT_PIPELINE") or "bot2")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
