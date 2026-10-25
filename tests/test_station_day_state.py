"""
Daily extrema (StationDayState), remark-group parsing, the terminal-aware
decision, and the bot2 producer wired to a METAR.ws snapshot.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

os.environ.setdefault("DRY_RUN", "1")
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.metar_observation import MetarObservation  # noqa: E402
from core.metar_ws_snapshot import build_snapshot, write_snapshot_atomic  # noqa: E402
from core.rt_daily_extrema import RESOLUTION_MODEL_DAILY_EXTREMA, decide_extrema  # noqa: E402
from core.station_day_state import (  # noqa: E402
    DayStateTracker,
    StationDayState,
    local_date_for,
    parse_remarks,
    precise_temperature_c,
)

NY = "America/New_York"


def obs(icao: str, when: str, temp_c: float, raw: str = "", speci: bool = False) -> MetarObservation:
    t = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(timezone.utc)
    kind = "SPECI" if speci else "METAR"
    raw = raw or f"{kind} {icao} {t:%d%H%M}Z 27010KT 10SM FEW050 {int(temp_c):02d}/06 A3012"
    return MetarObservation(icao, temp_c, when, raw, t, speci, source="test")


class TestRemarks(unittest.TestCase):
    def test_groups(self):
        rm = parse_remarks("METAR KNYC 251851Z 27010KT 10SM FEW050 17/06 A3012 RMK AO2 SLP201 T01720061 10183 20094 401830072")
        self.assertEqual((rm.temp_tenths_c, rm.dewpoint_tenths_c), (17.2, 6.1))
        self.assertEqual((rm.six_hour_max_c, rm.six_hour_min_c), (18.3, 9.4))
        self.assertEqual((rm.day_max_c, rm.day_min_c), (18.3, 7.2))

    def test_negative_and_absent(self):
        rm = parse_remarks("METAR KORD 251951Z 36015KT 10SM OVC020 M03/M08 A3030 RMK AO2 T10281083 11006 21033")
        self.assertEqual((rm.temp_tenths_c, rm.dewpoint_tenths_c), (-2.8, -8.3))
        self.assertEqual((rm.six_hour_max_c, rm.six_hour_min_c), (-0.6, -3.3))
        self.assertIsNone(rm.day_max_c)
        self.assertEqual(parse_remarks("METAR EGLL 251450Z 24008KT 9999 SCT030 12/08 Q1015"), parse_remarks(""))

    def test_body_numbers_are_not_mistaken_for_groups(self):
        # "10SM" and "A3012" live before RMK; only the remark section is parsed
        rm = parse_remarks("METAR KNYC 251851Z 27010KT 10SM FEW050 17/06 A3012")
        self.assertIsNone(rm.six_hour_max_c)
        self.assertIsNone(rm.temp_tenths_c)

    def test_precise_temperature_prefers_consistent_tenths(self):
        o = obs("KNYC", "2026-10-25T18:51:00Z", 17, "METAR KNYC 251851Z 17/06 A3012 RMK AO2 T01720061")
        self.assertEqual(precise_temperature_c(o), 17.2)
        bad = obs("KNYC", "2026-10-25T18:51:00Z", 17, "METAR KNYC 251851Z 17/06 A3012 RMK AO2 T02720061")
        self.assertEqual(precise_temperature_c(bad), 17.0)


class TestStationDayState(unittest.TestCase):
    def test_local_day_boundary(self):
        # 03:51Z on the 26th is still the evening of the 25th in New York (EDT, UTC-4)
        self.assertEqual(local_date_for(datetime(2026, 10, 26, 3, 51, tzinfo=timezone.utc), NY).isoformat(), "2026-10-25")
        self.assertEqual(local_date_for(datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc), NY).isoformat(), "2026-10-26")

    def test_running_extremes_speci_and_dedupe(self):
        tr = DayStateTracker(lambda icao: NY)
        tr.ingest(obs("KNYC", "2026-10-25T12:51:00Z", 12))
        tr.ingest(obs("KNYC", "2026-10-25T13:51:00Z", 14))
        tr.ingest(obs("KNYC", "2026-10-25T14:23:00Z", 19, speci=True))  # off-schedule peak: leading only
        tr.ingest(obs("KNYC", "2026-10-25T14:51:00Z", 18))
        tr.ingest(obs("KNYC", "2026-10-25T14:51:00Z", 18))  # duplicate frame
        st = tr.get("KNYC", "2026-10-25")
        # resolution extreme ignores SPECI (market rule), the leading extreme keeps it
        self.assertEqual((st.max_c, st.max_source, st.max_time_utc), (18.0, "body", "2026-10-25T14:51:00Z"))
        self.assertEqual((st.lead_max_c, st.lead_max_is_speci, st.lead_max_time_utc), (19.0, True, "2026-10-25T14:23:00Z"))
        self.assertFalse(st.max_is_speci)
        self.assertEqual((st.min_c, st.min_source), (12.0, "body"))
        self.assertEqual((st.obs_count, st.speci_count), (3, 1))
        self.assertEqual(st.last_temp_c, 18.0)
        self.assertEqual([s[1] for s in st.samples], [12.0, 14.0, 19.0, 18.0])
        # evening report after midnight UTC still belongs to the 25th locally
        tr.ingest(obs("KNYC", "2026-10-26T02:51:00Z", 9))
        self.assertEqual(tr.get("KNYC", "2026-10-25").min_c, 9.0)
        self.assertIsNone(tr.get("KNYC", "2026-10-26"))
        self.assertEqual(len(tr.days_for("KNYC")), 1)

    def test_six_hour_group_recorded_not_applied(self):
        tr = DayStateTracker(lambda icao: NY)
        tr.ingest(obs("KNYC", "2026-10-25T17:51:00Z", 17, "METAR KNYC 251751Z 17/06 A3012 RMK AO2 T01720061 10194"))
        st = tr.get("KNYC", "2026-10-25")
        self.assertEqual((st.max_c, st.max_source), (17.2, "t_group"))  # tenths from the T group
        self.assertEqual(st.reported_6h_max_c, 19.4)

    def test_24h_group_recorded_not_applied(self):
        tr = DayStateTracker(lambda icao: NY)
        tr.ingest(obs("KNYC", "2026-10-26T04:51:00Z", 10, "METAR KNYC 260451Z 10/05 A3012 RMK AO2 T01000050 401830072"))
        st = tr.get("KNYC", "2026-10-26")
        self.assertEqual((st.reported_24h_max_c, st.reported_24h_min_c), (18.3, 7.2))
        self.assertEqual(st.max_c, 10.0)

    def test_roundtrip_restore_and_prune(self):
        tr = DayStateTracker(lambda icao: NY, keep_days=2)
        tr.ingest(obs("KNYC", "2026-10-23T15:51:00Z", 20))
        tr.ingest(obs("KNYC", "2026-10-25T15:51:00Z", 15, speci=True))
        payload = tr.to_dict()
        self.assertEqual(set(payload["KNYC"]), {"2026-10-23", "2026-10-25"})
        tr2 = DayStateTracker(lambda icao: NY, keep_days=2)
        self.assertEqual(tr2.restore(json.loads(json.dumps(payload))), 2)
        st = tr2.get("KNYC", "2026-10-25")
        self.assertIsInstance(st, StationDayState)
        self.assertEqual((st.max_c, st.lead_max_c, st.speci_count), (None, 15.0, 1))
        # a replay of an already-known sample after restore is ignored (SPECI counted once, never as routine)
        tr2.ingest(obs("KNYC", "2026-10-25T15:51:00Z", 15, speci=True))
        self.assertEqual((tr2.get("KNYC", "2026-10-25").obs_count, tr2.get("KNYC", "2026-10-25").speci_count), (0, 1))
        removed = tr2.prune(now_utc=datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(removed, 1)
        self.assertIsNone(tr2.get("KNYC", "2026-10-23"))
        self.assertEqual(StationDayState.from_dict({"icao": "X"}), None)

    def test_restore_drops_states_from_another_timezone(self):
        tr = DayStateTracker(lambda icao: "UTC")
        tr.ingest(obs("KNYC", "2026-10-25T15:51:00Z", 20))
        payload = tr.to_dict()
        tr2 = DayStateTracker(lambda icao: NY)
        self.assertEqual(tr2.restore(payload), 0)
        self.assertEqual(len(tr2), 0)


class TestDecideExtrema(unittest.TestCase):
    def setUp(self):
        self.noon = datetime(2026, 10, 25, 12, 0, tzinfo=ZoneInfo(NY))
        self.late = datetime(2026, 10, 25, 23, 59, tzinfo=ZoneInfo(NY))
        self.near = datetime(2026, 10, 25, 22, 30, tzinfo=ZoneInfo(NY))

    def _d(self, **kw):
        base = dict(variable="temp_max", yes_if_above=True, threshold=70.0, extreme_value=65.0, local_now=self.noon)
        base.update(kw)
        return decide_extrema(**base)

    def test_max_above(self):
        self.assertEqual(self._d(extreme_value=71.0).binary_state, "met")
        self.assertEqual(self._d(extreme_value=70.0).recommended_side, "YES")
        self.assertEqual(self._d().binary_state, "open")
        self.assertEqual(self._d(local_now=self.late).binary_state, "dead")
        self.assertEqual(self._d(local_now=self.near, extreme_value=50.0).binary_state, "dead")
        self.assertEqual(self._d(local_now=self.near, extreme_value=60.0).binary_state, "open")

    def test_max_below_is_not_met_until_day_ends(self):
        # "high stays under 70": a max of 65 at noon proves nothing yet
        d = self._d(yes_if_above=False)
        self.assertEqual((d.binary_state, d.p_win, d.recommended_side), ("open", 0.5, "NONE"))
        self.assertEqual(self._d(yes_if_above=False, extreme_value=70.0).binary_state, "dead")
        self.assertEqual(self._d(yes_if_above=False, local_now=self.late).binary_state, "met")

    def test_min_markets(self):
        # YES if min < 40: a min of 38 is terminal
        self.assertEqual(self._d(variable="temp_min", yes_if_above=False, threshold=40.0, extreme_value=38.0).binary_state, "met")
        self.assertEqual(self._d(variable="temp_min", yes_if_above=False, threshold=40.0, extreme_value=45.0).binary_state, "open")
        # YES if min >= 40: a min of 38 kills it; a min of 45 only wins at day end
        self.assertEqual(self._d(variable="temp_min", yes_if_above=True, threshold=40.0, extreme_value=38.0).binary_state, "dead")
        self.assertEqual(self._d(variable="temp_min", yes_if_above=True, threshold=40.0, extreme_value=45.0).binary_state, "open")
        self.assertEqual(
            self._d(variable="temp_min", yes_if_above=True, threshold=40.0, extreme_value=45.0, local_now=self.late).binary_state,
            "met",
        )

    def test_bad_deadline_never_kills(self):
        self.assertEqual(self._d(deadline_hhmm="garbage", local_now=self.late).binary_state, "open")


class TestBot2ProducerWithSnapshot(unittest.TestCase):
    """bot2 produce_rt_signals reads the METAR.ws snapshot and decides on the daily max."""

    def _run(self, mapping: dict, day_rows: dict, latest_temp_c: float = 20.0):
        import bots.bot2_realtime.metar_live.produce_rt_signals as prod

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            paths = {
                "mapping": base / "icao_mapping.json",
                "signals": base / "realtime_signals.json",
                "meta": base / "realtime_signals_meta.json",
                "db": base / "portfolio_rt.sqlite",
            }
            paths["mapping"].write_text(json.dumps(mapping), encoding="utf-8")
            snap = base / "metar_ws_latest.json"
            now = datetime.now(timezone.utc)
            latest = {"KORD": obs("KORD", now.strftime("%Y-%m-%dT%H:%M:%SZ"), latest_temp_c)}
            write_snapshot_atomic(snap, build_snapshot(latest, connected=True, now=now, days={"KORD": day_rows}))
            env = {"METAR_SOURCE": "metar_ws", "METAR_WS_SNAPSHOT_FILE": str(snap), "METAR_WS_FALLBACK_AWC": "0"}
            with mock.patch.dict(os.environ, env), mock.patch.object(prod, "get_rt_paths", lambda: paths):
                prod.run_realtime_signals()
            return json.loads(paths["signals"].read_text(encoding="utf-8")), json.loads(paths["meta"].read_text(encoding="utf-8"))

    def _mapping(self, *, yes_if_above: bool, threshold_f: float = 80.0):
        today = datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
        return {
            "_rt_note": {"x": "y"},
            "0xmarket": {
                "name": "Chicago high",
                "icao": "KORD",
                "threshold": threshold_f,
                "threshold_unit": "F",
                "yes_if_above": yes_if_above,
                "timezone": "America/Chicago",
                "deadline_local_hhmm": "23:59",
                "target_date": today,
            },
        }, today

    def _day_rows(self, today: str, max_c: float):
        st = StationDayState(icao="KORD", local_date=today, timezone="America/Chicago")
        st.max_c, st.max_time_utc, st.max_source, st.max_raw = max_c, "2026-10-25T19:51:00Z", "t_group", "METAR KORD ..."
        st.min_c, st.min_source, st.obs_count, st.speci_count, st.last_temp_c = 10.0, "metar", 8, 1, 20.0
        return {today: st.to_dict()}

    def test_above_market_met_from_daily_max_even_if_instant_is_lower(self):
        mapping, today = self._mapping(yes_if_above=True)
        signals, meta = self._run(mapping, self._day_rows(today, 27.2), latest_temp_c=20.0)  # 27.2C = 81.0F
        row = signals["0xmarket"]
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(row["resolution_model"], RESOLUTION_MODEL_DAILY_EXTREMA)
        self.assertEqual((row["binary_state"], row["p_win"], row["recommended_side"]), ("met", 1.0, "YES"))
        self.assertEqual(row["current_obs_temp"], 81.0)
        self.assertEqual(row["instant_obs_temp"], 68.0)
        self.assertFalse(row["day_extreme"]["is_speci"])
        self.assertEqual(row["day_extreme"]["obs_count"], 8)

    def test_below_market_dead_once_max_crossed(self):
        mapping, today = self._mapping(yes_if_above=False)
        signals, _ = self._run(mapping, self._day_rows(today, 27.2))
        self.assertEqual((signals["0xmarket"]["binary_state"], signals["0xmarket"]["recommended_side"]), ("dead", "NO"))

    def test_without_day_state_falls_back_to_instant_metar(self):
        mapping, today = self._mapping(yes_if_above=True)
        signals, _ = self._run(mapping, {}, latest_temp_c=27.2)
        row = signals["0xmarket"]
        self.assertEqual(row["resolution_model"], "instant_metar_proxy")
        self.assertEqual(row["binary_state"], "met")
        self.assertIsNone(row["day_extreme"])

    def test_stale_snapshot_marks_metar_unavailable(self):
        import bots.bot2_realtime.metar_live.produce_rt_signals as prod

        mapping, today = self._mapping(yes_if_above=True)
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            paths = {"mapping": base / "m.json", "signals": base / "s.json", "meta": base / "meta.json", "db": base / "db"}
            paths["mapping"].write_text(json.dumps(mapping), encoding="utf-8")
            snap = base / "snap.json"
            old = datetime.now(timezone.utc) - timedelta(hours=2)
            write_snapshot_atomic(snap, build_snapshot({}, connected=False, now=old, days=self._day_rows(today, 27.2)))
            env = {"METAR_SOURCE": "metar_ws", "METAR_WS_SNAPSHOT_FILE": str(snap), "METAR_WS_FALLBACK_AWC": "0"}
            with mock.patch.dict(os.environ, env), mock.patch.object(prod, "get_rt_paths", lambda: paths):
                prod.run_realtime_signals()
            row = json.loads(paths["signals"].read_text(encoding="utf-8"))["0xmarket"]
        self.assertEqual(row["binary_state"], "metar_unavailable")


if __name__ == "__main__":
    unittest.main()
