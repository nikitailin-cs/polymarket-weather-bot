#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

mkdir -p "logs"

PYTHON_BIN="${ROOT_DIR}/.venv/bin/python3"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

"${PYTHON_BIN}" "bots/bot1/noaa_nomads_live/run_nomads_live_test.py" && \
"${PYTHON_BIN}" "bots/bot1/execution/finance_engine.py" >> "logs/finance.log" 2>&1
