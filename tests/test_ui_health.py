"""Topbar health, settings masking and the new endpoints."""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("DRY_RUN", "1")
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ui import data as ud  # noqa: E402
from ui.health import ConnectionHealth, mask, metar_ws_view, settings_view  # noqa: E402

NOW = datetime(2026, 10, 25, 18, 5, tzinfo=timezone.utc)


class TestMask(unittest.TestCase):
    def test_secrets_masked_values_kept(self):
        self.assertEqual(mask("METAR_WS_API_KEY", "mts_live_ABCDEFGHIJKLMNOP"), "mts_…OP")
        self.assertEqual(mask("PRIVATE_KEY", "0xabc"), "•••")
        self.assertEqual(mask("DRY_RUN", "1"), "1")
        self.assertIsNone(mask("UI_TOKEN", None))


class TestMetarView(unittest.TestCase):
    def _snap(self, gen, connected=True):
        return {"generated_at_utc": gen.strftime("%Y-%m-%dT%H:%M:%SZ"), "connected": connected,
                "client": {"plan": "trader", "limits": {"max_channels": 1000}, "subscribed": ["a", "b"], "wanted_channels": ["a", "b", "c"],
                           "last_message_at_utc": (gen - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ"), "reconnects": 2, "forecast_records": 84, "messages": 50},
                "stations": {"KORD": {}}}

    def test_statuses(self):
        ok = metar_ws_view(self._snap(NOW - timedelta(seconds=10)), NOW)
        self.assertEqual((ok["status"], ok["channels_subscribed"], ok["channels_wanted"], ok["plan"]), ("ok", 2, 3, "trader"))
        self.assertEqual(ok["last_message_age_sec"], 15.0)
        # cleanly stopped daemon (messages flowed, no error) within 30 min: still green
        self.assertEqual(metar_ws_view(self._snap(NOW - timedelta(seconds=10), connected=False), NOW)["status"], "ok")
        broken = self._snap(NOW - timedelta(seconds=10), connected=False); broken["client"]["last_error"] = "TimeoutError"
        self.assertEqual(metar_ws_view(broken, NOW)["status"], "disconnected")
        self.assertEqual(metar_ws_view(self._snap(NOW - timedelta(seconds=600)), NOW)["status"], "ok")  # connected: green even when quiet
        self.assertEqual(metar_ws_view(self._snap(NOW - timedelta(seconds=2000)), NOW)["status"], "stale")  # > 30 min silent: yellow
        self.assertEqual(metar_ws_view(None, NOW)["status"], "no_snapshot")


class TestSettingsView(unittest.TestCase):
    def test_sources_and_masking(self):
        with mock.patch.dict(os.environ, {"DRY_RUN": "1", "METAR_WS_API_KEY": "mts_live_SECRETSECRET"}):
            v = settings_view(paths={"snapshot": "x"}, mapping_stats={"markets": 3}, mode="paper")
        rows = {r["name"]: r for r in v["values"]}
        self.assertEqual((rows["DRY_RUN"]["value"], rows["DRY_RUN"]["source"], rows["DRY_RUN"]["secret"]), ("1", "env", False))
        self.assertTrue(rows["METAR_WS_API_KEY"]["secret"])
        self.assertNotIn("SECRETSECRET", json.dumps(v))
        self.assertEqual(v["mode"], "paper")


class TestHealthProbe(unittest.TestCase):
    def test_probe_records_down_without_network(self):
        h = ConnectionHealth(interval_sec=1, gamma_url="http://127.0.0.1:9", clob_url="http://127.0.0.1:9")
        h.probe_once()
        v = h.view()
        self.assertEqual((v["gamma"]["status"], v["clob"]["status"]), ("down", "down"))
        self.assertTrue(v["gamma"]["error"])


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestEndpoints(unittest.TestCase):
    def test_connections_and_settings(self):
        from ui.server import serve

        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            snap = base / "snap.json"
            snap.write_text(json.dumps({"generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "connected": True,
                                        "client": {"plan": "trader", "subscribed": [], "wanted_channels": []}, "stations": {}, "days": {}}), encoding="utf-8")
            mapping = base / "m.json"
            mapping.write_text(json.dumps({"0x1": {"icao": "KORD", "target_date": "2026-10-25", "refreshed_at": "2026-10-25T10:00:00Z", "threshold": 1}}), encoding="utf-8")
            env = {"UI_POLYMARKET_PRICES": "0", "METAR_SOURCE": "metar_ws", "DRY_RUN": "1"}
            with mock.patch.dict(os.environ, env), mock.patch.object(ud, "mapping_path", lambda: mapping), \
                 mock.patch.object(ud, "signals_paths", lambda: (base / "s.json", base / "meta.json")):
                dash = ud.Dashboard(snapshot_file=snap, prices=ud.GammaPrices(), health=ConnectionHealth(gamma_url="http://127.0.0.1:9", clob_url="http://127.0.0.1:9"))
                port = _free_port()
                httpd = serve("127.0.0.1", port, dashboard=dash)
                th = threading.Thread(target=httpd.serve_forever, daemon=True); th.start()
                try:
                    url = f"http://127.0.0.1:{port}"
                    c = json.loads(urllib.request.urlopen(f"{url}/api/connections").read())
                    self.assertEqual(c["metar_ws"]["status"], "ok")
                    self.assertEqual(c["gamma"]["status"], "unknown")  # probe thread not started in tests
                    self.assertEqual(c["mode"], "paper")
                    s = json.loads(urllib.request.urlopen(f"{url}/api/settings").read())
                    self.assertEqual(s["mapping"]["markets"], 1)
                    self.assertEqual(s["mapping"]["stations"], 1)
                    self.assertIn("snapshot", s["paths"])
                    html = urllib.request.urlopen(f"{url}/").read().decode()
                    for needle in ("#dashboard", "#markets", "#stations", "#logs", "#settings", 'id="clock"', 'id="c-metar"', 'id="c-gamma"', 'id="c-clob"'):
                        self.assertIn(needle, html)
                    ov = json.loads(urllib.request.urlopen(f"{url}/api/overview").read())
                    self.assertIn("connections", ov)
                finally:
                    httpd.shutdown(); httpd.server_close()


if __name__ == "__main__":
    unittest.main()
