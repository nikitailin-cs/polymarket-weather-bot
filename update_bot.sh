#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

mkdir -p "logs"

PYTHON_BIN="${ROOT_DIR}/.venv/bin/python3"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

NOMADS_EXTRA=()
if [[ "${NOMADS_ALLOW_OUTSIDE_WINDOW:-0}" == "1" ]]; then
  NOMADS_EXTRA+=(--allow-outside-window)
fi
if [[ "${NOMADS_FORCE:-0}" == "1" ]]; then
  NOMADS_EXTRA+=(--force)
fi

exit_code=0
set +e
"${PYTHON_BIN}" "bots/bot1/noaa_nomads_live/run_nomads_live_test.py" "${NOMADS_EXTRA[@]}"
noaa_ec=$?
set -e
if [[ "${noaa_ec}" -ne 0 ]]; then
  exit_code="${noaa_ec}"
  echo "[update_bot] NOAA terminé avec code ${noaa_ec} — finance_engine lancé quand même" >&2
fi

set +e
"${PYTHON_BIN}" "bots/bot1/execution/finance_engine.py" >> "logs/finance.log" 2>&1
fin_ec=$?
set -e
if [[ "${fin_ec}" -ne 0 ]]; then
  exit_code="${fin_ec}"
fi

exit "${exit_code}"
