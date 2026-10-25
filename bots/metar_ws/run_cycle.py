#!/usr/bin/env python3
"""
One real-time trading cycle, cross-platform (replaces ``update_bot_rt.sh`` where
bash or ``python3`` are not available, e.g. Windows):

1. ``produce_rt_signals.py``  (observations -> realtime_signals.json)
2. manifest check             (status == ok)
3. ``finance_engine_rt.py``   (signals -> CLOB orders, or paper fills in DRY_RUN)
4. paper settlement           (OPEN paper positions -> SETTLED via Gamma), unless
   ``UPDATE_BOT_RT_SKIP_SETTLEMENT=1``; the on-chain settlement chain is not run here.

Writes the same ``cycle_summary {json}`` line as the shell script to
``logs/update_bot_rt_cycle.log`` so the dashboard history sees both.
Always runs the child steps with this interpreter (``sys.executable``).

    python bots/metar_ws/run_cycle.py
    METAR_WS_TRIGGER_CMD="python bots/metar_ws/run_cycle.py"   # daemon push mode
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

LOG = logging.getLogger("run_cycle")


def _cycle_log() -> Path:
    raw = (os.getenv("UPDATE_BOT_RT_CYCLE_LOG") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else _ROOT / p
    return _ROOT / "logs" / "update_bot_rt_cycle.log"


def _step_timeout() -> Optional[float]:
    raw = (os.getenv("RUN_CYCLE_STEP_TIMEOUT_SEC") or "150").strip()
    try:
        v = float(raw)
    except ValueError:
        v = 150.0
    return v if v > 0 else None


def _run(script: str, log: Path) -> int:
    """Run one step with this interpreter; a step over RUN_CYCLE_STEP_TIMEOUT_SEC is killed (rc 124)."""
    cmd = [sys.executable, str(_ROOT / script)]
    started = time.monotonic()
    with log.open("a", encoding="utf-8") as f:
        f.write(f"--- {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {script}\n")
        f.flush()
        try:
            rc = subprocess.call(cmd, cwd=str(_ROOT), stdout=f, stderr=subprocess.STDOUT, timeout=_step_timeout())
        except subprocess.TimeoutExpired:
            rc = 124
            f.write(f"[run_cycle] {script} killed after {_step_timeout()}s (RUN_CYCLE_STEP_TIMEOUT_SEC)\n")
    LOG.info("%s rc=%s (%.1fs)", script, rc, time.monotonic() - started)
    return rc


def _manifest_ok(meta_path: Path) -> bool:
    try:
        m = json.loads(meta_path.read_text(encoding="utf-8"))
        return str(m.get("status", "")).strip() == "ok"
    except (OSError, ValueError):
        return False


def run_cycle(pipeline: Optional[str] = None) -> int:
    pipeline = (pipeline or os.getenv("UPDATE_BOT_RT_PIPELINE") or "bot2").strip().lower()
    log = _cycle_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    if pipeline == "bot1":
        from core.bot1_realtime_data_paths import realtime_signals_meta_path

        produce = "bots/bot1_realtime/metar_live/produce_rt_signals.py"
        finance = "bots/bot1_realtime/execution/finance_engine_rt.py"
        meta = realtime_signals_meta_path()
    else:
        from core.bot2_data_paths import get_rt_paths

        produce = "bots/bot2_realtime/metar_live/produce_rt_signals.py"
        finance = "bots/bot2_realtime/execution/finance_engine_rt.py"
        meta = get_rt_paths()["meta"]

    with log.open("a", encoding="utf-8") as f:
        f.write(f"=== [run_cycle] start {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} pipeline={pipeline} ===\n")

    metar_rc = _run(produce, log)
    fin_rc: Optional[int] = None
    fin_skipped = True
    if metar_rc == 0 and _manifest_ok(meta):
        fin_rc = _run(finance, log)
        fin_skipped = False
    elif metar_rc == 0:
        LOG.warning("manifest not ok (%s): finance engine skipped", meta)

    settle_rc = 0
    settle: dict[str, Any] = {}
    if (os.getenv("UPDATE_BOT_RT_SKIP_SETTLEMENT") or "0").strip() != "1":
        from execution.paper_broker import paper_enabled, paper_portfolio_path, settle_paper_positions

        if paper_enabled():
            try:
                settle = settle_paper_positions(paper_portfolio_path())
                LOG.info("paper settlement: %s", settle)
            except Exception as e:  # noqa: BLE001
                LOG.error("paper settlement failed: %s", e)
                settle_rc = 1

    agg = metar_rc if metar_rc != 0 else (fin_rc or 0) if fin_rc else settle_rc
    summary = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pipeline": pipeline,
        "runner": "run_cycle.py",
        "step_exit_codes": {
            "metar": metar_rc,
            "finance_rt": fin_rc,
            "finance_rt_skipped": fin_skipped,
            "settlement_chain": settle_rc,
        },
        "paper_settlement": settle or None,
        "aggregated_exit_code": agg,
    }
    with log.open("a", encoding="utf-8") as f:
        f.write(f"[run_cycle] cycle_summary {json.dumps(summary, ensure_ascii=False)}\n")
    LOG.info("cycle_summary %s", json.dumps(summary, ensure_ascii=False))
    return agg


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    try:
        from dotenv import load_dotenv

        load_dotenv(_ROOT / ".env")
    except Exception:  # noqa: BLE001
        pass
    return run_cycle()


if __name__ == "__main__":
    raise SystemExit(main())
