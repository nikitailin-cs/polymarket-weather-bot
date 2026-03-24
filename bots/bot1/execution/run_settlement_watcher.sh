#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT_DIR"

mkdir -p "logs"

PYTHON_BIN="${ROOT_DIR}/.venv/bin/python3"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

# Watcher settlement: OPEN -> RESOLVED
"${PYTHON_BIN}" "bots/bot1/execution/settlement_watcher.py" >> "logs/settlement_watcher.log" 2>&1
