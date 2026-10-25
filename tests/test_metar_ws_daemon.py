"""
Daemon-level test: fake METAR.ws server -> Daemon writes the snapshot that the
RT producers read, and runs the trigger command once per live observation burst.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DRY_RUN", "1")
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bots.metar_ws.metar_ws_daemon import Daemon, stations_from_mapping  # noqa: E402
from core.metar_ws_client import MetarWsConfig  # noqa: E402
from core.metar_ws_snapshot import MetarWsSnapshotRegistry, read_snapshot  # noqa: E402

HAVE_WEBSOCKETS = importlib.util.find_spec("websockets") is not None

FRAME = {
    "type": "publication",
    "channel": "metar.obs.kord",
    "cached": False,
    "data": {
        "icao": "KORD",
        "observation_time": "2026-10-25T14:51:00Z",
        "raw": "METAR KORD 251451Z 18012KT 10SM BKN045 21/12 A2998",
        "temperature_c": 21,
    },
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestMapping(unittest.TestCase):
    def test_stations_from_mapping_skips_notes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "icao_mapping.json"
            p.write_text(
                json.dumps(
                    {
                        "_rt_note": {"x": "y"},
                        "0xaaa": {"icao": "KORD", "threshold": 80},
                        "0xbbb": {"icao": "knyc"},
                        "0xccc": {"threshold": 1},
                        "0xddd": "garbage",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(stations_from_mapping(p), {"KORD", "KNYC"})
            self.assertEqual(stations_from_mapping(Path(d) / "missing.json"), set())


@unittest.skipUnless(HAVE_WEBSOCKETS, "websockets not installed")
class TestDaemon(unittest.TestCase):
    def test_snapshot_and_trigger(self):
        import websockets

        port = _free_port()

        async def handler(ws, *_):
            await ws.send(json.dumps({"type": "ack", "plan": "pro", "limits": {"max_channels": 100}}))
            async for frame in ws:
                msg = json.loads(frame)
                if msg.get("action") == "subscribe":
                    await ws.send(json.dumps({"type": "subscribed", "channels": msg["channels"]}))
                    await ws.send(json.dumps(dict(FRAME, cached=True)))
                    for i, t in enumerate((22, 23)):  # a burst of two live reports
                        live = json.loads(json.dumps(FRAME))
                        live["data"]["observation_time"] = f"2026-10-25T15:{10 + i:02d}:00Z"
                        live["data"]["temperature_c"] = t
                        await ws.send(json.dumps(live))

        with tempfile.TemporaryDirectory() as d:
            snap = Path(d) / "rt" / "metar_ws_latest.json"
            marker = Path(d) / "trigger_calls.txt"
            trigger = (
                f'"{sys.executable}" -c "import pathlib; p=pathlib.Path(r\'{marker}\'); '
                f"p.write_text(p.read_text()+'x' if p.exists() else 'x')\""
            )

            async def go():
                async with websockets.serve(handler, "127.0.0.1", port):
                    cfg = MetarWsConfig(api_key="k", url=f"ws://127.0.0.1:{port}/v1/ws", reconnect_min_sec=0.1, forecasts=False)
                    daemon = Daemon(
                        config=cfg,
                        fixed_stations={"KORD"},
                        snapshot_file=snap,
                        trigger_cmd=trigger,
                        trigger_debounce_sec=0.3,
                        heartbeat_sec=5,
                        events_file=Path(d) / "events.jsonl",
                    )
                    await daemon.run(max_seconds=2.5)
                    return daemon

            daemon = asyncio.run(go())

            self.assertEqual(daemon.live_observations, 2)
            self.assertEqual(daemon.triggers_run, 1, "burst of two reports must coalesce into one run")
            self.assertEqual(marker.read_text(), "x")
            events = [json.loads(l) for l in (Path(d) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([e["kind"] for e in events], ["connected", "observation", "observation", "trigger", "disconnected"])
            self.assertEqual(events[0]["plan"], "pro")
            self.assertEqual(events[3]["rc"], 0)

            data = read_snapshot(snap)
            self.assertIsNotNone(data)
            self.assertEqual(data["client"]["plan"], "pro")
            self.assertEqual(data["stations"]["KORD"]["temp_c"], 23.0)
            self.assertEqual(data["client"]["wanted_channels"], ["metar.obs.kord"])
            self.assertEqual(data["client"]["subscribed"], [], "subscriptions are cleared on disconnect")
            self.assertFalse(data["connected"], "final snapshot is written after shutdown")
            day = data["days"]["KORD"]
            self.assertEqual(len(day), 1)
            (st,) = day.values()
            self.assertEqual(st["timezone"], "America/Chicago")  # from data/metar_ws_stations.json
            self.assertEqual((st["max_c"], st["min_c"], st["obs_count"]), (23.0, 21.0, 3))
            self.assertEqual(MetarWsSnapshotRegistry(path=snap, max_age_sec=3600).fetch_day_state("KORD", st["local_date"]).max_c, 23.0)

            # the bridge the producers use reads it back (allow the big age so the test is time-independent)
            reg = MetarWsSnapshotRegistry(path=snap, max_age_sec=3600, obs_max_age_sec=0)
            self.assertEqual(reg.fetch_latest_metar("KORD")["temp_f"], 73.4)
            self.assertEqual(reg.fetch_latest_for_stations(["KORD"])["KORD"].temp_celsius, 23.0)


if __name__ == "__main__":
    unittest.main()
