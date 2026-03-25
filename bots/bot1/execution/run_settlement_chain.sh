#!/usr/bin/env bash
# Chaîne settlement seule : OPEN -> RESOLVED (watcher) puis RESOLVED -> SETTLED (exit_engine).
# Utile si NOAA + finance_engine sont lancés ailleurs (ex. timer systemd dédié).
# Déploiement standard : préférer `update_bot.sh` qui inclut déjà cette chaîne.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT_DIR"

mkdir -p "logs"

PYTHON_BIN="${ROOT_DIR}/.venv/bin/python3"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

"${PYTHON_BIN}" "bots/bot1/execution/settlement_watcher.py" >> "logs/settlement_watcher.log" 2>&1
"${PYTHON_BIN}" "bots/bot1/execution/exit_engine.py" >> "logs/exit_engine.log" 2>&1
