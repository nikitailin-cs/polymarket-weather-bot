"""History screen: positions from SQLite, cycle-log parsing, jsonl logs, alert persistence."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("DRY_RUN", "1")
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.bot2_portfolio_schema import ensure_bot2_positions_table, insert_open_position  # noqa: E402
from ui import history as h  # noqa: E402

NOW = datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc)


class TestPositions(unittest.TestCase):
    def test_read_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "portfolio_rt.sqlite"
            conn = sqlite3.connect(db)
            try:
                ensure_bot2_positions_table(conn)
                insert_open_position(conn, market_id="0xaaa", side="YES", price=0.94, size=50, timestamp_iso_utc="2026-10-25T18:10:00Z")
                insert_open_position(conn, market_id="0xbbb", side="NO", price=0.80, size=10, timestamp_iso_utc="2026-10-24T19:00:00Z")
                insert_open_position(conn, market_id="0xccc", side="YES", price=0.90, size=20, timestamp_iso_utc="2026-10-23T19:00:00Z")
                conn.execute("UPDATE positions SET status='SETTLED', pnl=2.0, settled_at='2026-10-25T03:00:00Z' WHERE market_id='0xbbb'")
                conn.execute("UPDATE positions SET status='SETTLED', pnl=-18.0, settled_at='2026-10-24T03:00:00Z' WHERE market_id='0xccc'")
                conn.commit()
            finally:
                conn.close()
            rows = h.read_positions(db)
            self.assertEqual([r["market_id"] for r in rows], ["0xaaa", "0xbbb", "0xccc"])  # newest first
            self.assertEqual(rows[0]["notional"], 47.0)
            self.assertEqual(rows[0]["max_payout"], 50.0)
            s = h.positions_summary(rows, NOW)
            self.assertEqual(s["by_status"], {"OPEN": 1, "SETTLED": 2})
            self.assertEqual(s["open_notional_usdc"], 47.0)
            self.assertEqual((s["realized_pnl_usdc"], s["realized_today_usdc"], s["wins"], s["losses"]), (-16.0, 2.0, 1, 1))
            self.assertEqual(h.read_positions(Path(d) / "missing.sqlite"), [])


class TestCycleLog(unittest.TestCase):
    LINES = [
        "=== [update_bot_rt] start 2026-10-25T18:52:00Z pipeline=bot2 ===",
        '[update_bot_rt] cycle_summary {"ts":"2026-10-25T18:52:40Z","pipeline":"bot2","step_exit_codes":{"metar":0,"finance_rt":0,"finance_rt_skipped":false,"settlement_chain":0},"aggregated_exit_code":0}',
        '{"ts":"2026-10-25T18:52:40Z","pipeline":"bot2","step_exit_codes":{"metar":0,"finance_rt":0,"finance_rt_skipped":false,"settlement_chain":0},"aggregated_exit_code":0}',
        '{"ts":"2026-10-25T18:55:40Z","pipeline":"bot2","step_exit_codes":{"metar":1,"finance_rt":null,"finance_rt_skipped":true,"settlement_chain":0},"aggregated_exit_code":1}',
        "garbage { not json",
    ]

    def test_parse_dedupes_and_orders(self):
        rows = h.parse_cycle_summaries(self.LINES)
        self.assertEqual([r["ts"] for r in rows], ["2026-10-25T18:55:40Z", "2026-10-25T18:52:40Z"])
        bad = rows[0]
        self.assertEqual((bad["metar"], bad["finance"], bad["finance_skipped"], bad["ok"]), (1, None, True, False))
        self.assertTrue(rows[1]["ok"])

    def test_tail_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cycle.log"
            p.write_text("\n".join(self.LINES) + "\n", encoding="utf-8")
            self.assertEqual(len(h._tail_lines(p, 2)), 2)
            self.assertEqual(h._tail_lines(Path(d) / "none.log", 5), [])


class TestJsonlAndAlerts(unittest.TestCase):
    def test_append_and_read(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "events.jsonl"
            h.append_jsonl(p, {"kind": "connected", "plan": "trader"})
            h.append_jsonl(p, {"kind": "observation", "icao": "KORD", "temp_c": 12.0})
            rows = h.read_jsonl(p)
            self.assertEqual([r["kind"] for r in rows][:2], ["observation", "connected"]) if rows[0]["ts"] != rows[1]["ts"] else self.assertEqual({r["kind"] for r in rows}, {"connected", "observation"})
            self.assertTrue(all("ts" in r for r in rows))
            self.assertEqual(h.read_jsonl(Path(d) / "none.jsonl"), [])

    def test_alert_persisted_even_without_telegram(self):
        import monitoring.telegram_alert as ta

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "alerts.jsonl"
            with mock.patch.object(ta, "_ALERTS_LOG", log), mock.patch.object(ta, "TELEGRAM_BOT_TOKEN", ""):
                self.assertFalse(ta.send_alert("hello <b>", "⚠️"))
            rows = h.read_jsonl(log)
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["message"], rows[0]["emoji"], rows[0]["sent"]), ("hello <b>", "⚠️", False))


class TestHistoryView(unittest.TestCase):
    def test_assembly(self):
        positions = [{"market_id": "0xaaa", "side": "YES", "price": 0.9, "size": 10, "notional": 9.0, "max_payout": 10.0,
                      "status": "OPEN", "opened_at": "2026-10-25T18:00:00Z", "settled_at": None, "pnl": None}]
        cycles = h.parse_cycle_summaries(TestCycleLog.LINES)
        v = h.history_view(positions=positions, names={"0xaaa": "Chicago high"}, cycles=cycles, alerts=[], events=[], now=NOW)
        self.assertEqual(v["positions"][0]["name"], "Chicago high")
        self.assertEqual(v["last_cycle"]["ts"], "2026-10-25T18:55:40Z")
        self.assertAlmostEqual(v["minutes_since_ok_cycle"], 67.3, places=1)
        self.assertEqual(v["summary"]["by_status"], {"OPEN": 1})


if __name__ == "__main__":
    unittest.main()
