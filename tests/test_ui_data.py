"""Dashboard data layer: freshness, station board, market join, server endpoints."""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("DRY_RUN", "1")
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ui import data as ud  # noqa: E402

NOW = datetime(2026, 10, 25, 18, 5, tzinfo=timezone.utc)
SERVED = {
    "KORD": {"name": "O'Hare", "city": "Chicago", "report_minutes": [51]},
    "EGLC": {"name": "London City", "city": "London", "report_minutes": [20, 50]},
}


def _snapshot(generated=NOW):
    return {
        "schema": "metar_ws_snapshot.v1",
        "generated_at_utc": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "connected": True,
        "client": {"plan": "trader", "limits": {"max_channels": 1000}, "subscribed": ["metar.obs.kord", "metar.obs.eglc"],
                   "wanted_channels": ["metar.obs.kord", "metar.obs.eglc", "metar.obs.zzzz"], "reconnects": 0, "failed_channels": {"metar.obs.zzzz": "channel_format"}},
        "stations": {
            "KORD": {"icao": "KORD", "temp_c": 21.0, "observation_time_utc": "2026-10-25T17:51:00Z", "received_at_utc": "2026-10-25T17:51:40Z",
                     "raw": "METAR KORD 251751Z 18012KT 10SM BKN045 21/12 A2998", "is_speci": False},
            "EGLC": {"icao": "EGLC", "temp_c": 12.0, "observation_time_utc": "2026-10-25T16:50:00Z", "received_at_utc": "2026-10-25T16:50:20Z",
                     "raw": "METAR EGLC 251650Z 24008KT 9999 SCT030 12/08 Q1015", "is_speci": False},
        },
        "days": {
            "KORD": {"2026-10-25": {"icao": "KORD", "local_date": "2026-10-25", "timezone": "America/Chicago", "max_c": 27.2, "max_time_utc": "2026-10-25T19:51:00Z",
                                    "max_source": "t_group", "min_c": 10.0, "min_time_utc": "2026-10-25T11:51:00Z", "min_source": "body", "obs_count": 8, "speci_count": 1,
                                    "lead_max_c": 27.8, "lead_max_time_utc": "2026-10-25T19:23:00Z", "lead_max_is_speci": True,
                                    "last_temp_c": 21.0, "samples": [["2026-10-25T11:51:00Z", 10.0, False], ["2026-10-25T19:23:00Z", 27.8, True]]}},
        },
    }


class TestFreshness(unittest.TestCase):
    def test_next_report_and_status(self):
        last = datetime(2026, 10, 25, 17, 51, tzinfo=timezone.utc)
        self.assertEqual(ud.next_report_after(last, [51]), datetime(2026, 10, 25, 18, 51, tzinfo=timezone.utc))
        self.assertEqual(ud.next_report_after(datetime(2026, 10, 25, 16, 50, tzinfo=timezone.utc), [20, 50]),
                         datetime(2026, 10, 25, 17, 20, tzinfo=timezone.utc))
        snap = _snapshot()
        ok = ud.station_freshness("KORD", snap["stations"]["KORD"], SERVED, NOW)
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["delivery_lag_sec"], 40.0)
        self.assertEqual(ok["next_expected_utc"], "2026-10-25T18:51:00Z")
        late = ud.station_freshness("EGLC", snap["stations"]["EGLC"], SERVED, NOW)  # 17:20 and 17:50 missed
        self.assertEqual(late["status"], "late")
        self.assertGreater(late["late_min"], 40)
        nodata = ud.station_freshness("ZZZZ", None, SERVED, NOW)
        self.assertEqual((nodata["status"], nodata["served"]), ("no_data", False))

    def test_stream_view(self):
        v = ud.stream_view(_snapshot(), SERVED, NOW)
        self.assertTrue(v["connected"])
        self.assertEqual(v["counts"], {"ok": 1, "late": 1, "no_data": 1})
        self.assertEqual([s["icao"] for s in v["stations"]], ["EGLC", "KORD", "ZZZZ"])
        self.assertFalse(ud.stream_view(None, SERVED, NOW)["available"])


class TestStationBoard(unittest.TestCase):
    def test_rows(self):
        rows = ud.station_board(_snapshot(), SERVED, NOW)
        by = {r["icao"]: r for r in rows}
        k = by["KORD"]
        self.assertEqual(k["local_time"], "13:05")  # CDT
        self.assertEqual(k["day"]["max_f"], 81.0)
        self.assertEqual((k["day"]["max_source"], k["day"]["lead_max_c"], k["day"]["lead_max_is_speci"]), ("t_group", 27.8, True))
        self.assertEqual(len(k["day"]["samples"]), 2)
        self.assertIsNone(by["EGLC"]["day"])


class TestMarkets(unittest.TestCase):
    MAPPING = {
        "_rt_note": {"x": "y"},
        "0xabc": {"name": "Chicago high ≥ 80F", "icao": "KORD", "threshold": 80, "threshold_unit": "F", "yes_if_above": True,
                  "timezone": "America/Chicago", "deadline_local_hhmm": "23:59", "target_date": "2026-10-25"},
        "0xdef": {"name": "Chicago high < 90F", "icao": "KORD", "threshold": 90, "threshold_unit": "F", "yes_if_above": False,
                  "timezone": "America/Chicago", "target_date": "2026-10-25"},
    }
    SIGNALS = {
        "0xabc": {"p_win": 1.0, "binary_state": "met", "recommended_side": "YES", "current_obs_temp": 81.0, "instant_obs_temp": 69.8,
                  "resolution_model": "metar_ws_daily_extrema"},
        "0xdef": {"p_win": 0.5, "binary_state": "open", "recommended_side": "NONE", "current_obs_temp": 81.0},
    }

    def test_join_and_edge(self):
        prices = {"0xabc": {"yes": 0.82}, "0xdef": {"yes": 0.9}}
        rows = ud.market_rows(self.MAPPING, self.SIGNALS, _snapshot(), prices, NOW)
        self.assertEqual([r["market_id"] for r in rows], ["0xdef", "0xabc"])  # open first
        a = next(r for r in rows if r["market_id"] == "0xabc")
        self.assertEqual((a["day_extreme"], a["resolution_value"], a["gap"]), (81.0, 81, 0.0))  # 81F sits inside ">= 80F"
        self.assertEqual(a["bucket"], {"low": 80.0, "high": None})
        self.assertEqual(a["lead_resolution_value"], 82)  # 27.8C -> 82.04F
        self.assertAlmostEqual(a["edge"], 0.18, places=3)
        self.assertEqual(a["minutes_to_deadline"], (23 - 13) * 60 + (59 - 5))
        d = next(r for r in rows if r["market_id"] == "0xdef")
        self.assertEqual((d["bucket"], d["gap"]), ({"low": None, "high": 89.0}, 0.0))  # "< 90F" holds while the max is 81
        self.assertIsNone(d["edge"])
        self.assertEqual(d["price"]["yes"], 0.9)

    def test_load_signals_both_shapes(self):
        with tempfile.TemporaryDirectory() as t:
            s, m = Path(t) / "s.json", Path(t) / "m.json"
            s.write_text(json.dumps(self.SIGNALS), encoding="utf-8")
            m.write_text(json.dumps({"status": "ok", "generated_at_utc": "2026-10-25T18:00:00Z"}), encoding="utf-8")
            sig, meta = ud.load_signals(s, m)
            self.assertEqual(set(sig), {"0xabc", "0xdef"})
            self.assertEqual(meta["status"], "ok")
            s.write_text(json.dumps({"last_run_utc": "x", "signals": [{"market_id": "0x1", "p_win": 0.5}]}), encoding="utf-8")
            m.unlink()
            sig, meta = ud.load_signals(s, m)
            self.assertEqual(list(sig), ["0x1"])
            self.assertEqual(meta["last_run_utc"], "x")
        self.assertEqual(ud.load_signals(Path("/nonexistent/s.json"), Path("/nonexistent/m.json")), ({}, {}))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestServer(unittest.TestCase):
    def test_endpoints_and_token(self):
        from ui.server import serve

        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            snap = base / "snap.json"
            snap.write_text(json.dumps(_snapshot(datetime.now(timezone.utc))), encoding="utf-8")
            mapping = base / "mapping.json"
            mapping.write_text(json.dumps(TestMarkets.MAPPING), encoding="utf-8")
            sig = base / "signals.json"
            sig.write_text(json.dumps(TestMarkets.SIGNALS), encoding="utf-8")
            env = {"UI_POLYMARKET_PRICES": "0", "METAR_SOURCE": "metar_ws"}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(ud, "mapping_path", lambda: mapping), \
                 mock.patch.object(ud, "signals_paths", lambda: (sig, base / "meta.json")), \
                 mock.patch.object(ud, "stations_file", lambda: _ROOT / "data" / "metar_ws_stations.json"):
                dash = ud.Dashboard(snapshot_file=snap, prices=ud.GammaPrices())
                port = _free_port()
                httpd = serve("127.0.0.1", port, token="s3cret", dashboard=dash)
                th = threading.Thread(target=httpd.serve_forever, daemon=True)
                th.start()
                try:
                    url = f"http://127.0.0.1:{port}"
                    html = urllib.request.urlopen(f"{url}/").read().decode()
                    self.assertIn("Weather Bot Ops", html)
                    with self.assertRaises(urllib.error.HTTPError) as cm:
                        urllib.request.urlopen(f"{url}/api/overview")
                    self.assertEqual(cm.exception.code, 401)
                    ov = json.loads(urllib.request.urlopen(f"{url}/api/overview?token=s3cret").read())
                    self.assertTrue(ov["stream"]["connected"])
                    self.assertEqual(len(ov["markets"]["rows"]), 2)
                    self.assertEqual(ov["markets"]["observation_source"], "metar_ws")
                    self.assertEqual(ov["stations"][0]["icao"], "EGLC")
                    req = urllib.request.Request(f"{url}/api/stream", headers={"Authorization": "Bearer s3cret"})
                    st = json.loads(urllib.request.urlopen(req).read())
                    self.assertEqual(st["client"]["plan"], "trader")
                    self.assertEqual(json.loads(urllib.request.urlopen(f"{url}/healthz").read()), {"ok": True})
                finally:
                    httpd.shutdown()
                    httpd.server_close()


if __name__ == "__main__":
    unittest.main()
