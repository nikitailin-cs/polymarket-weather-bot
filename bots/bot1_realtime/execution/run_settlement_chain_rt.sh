#!/usr/bin/env bash
# Phase 5 — Chaîne settlement **temps réel** (portfolio_rt.sqlite) :
#   1) settlement_watcher.py  : OPEN → RESOLVED (Gamma : marché résolu / clos)
#   2) exit_engine.py         : RESOLVED → redeem on-chain (Polygon) → SETTLED
#
# Usage:
#   ./bots/bot1_realtime/execution/run_settlement_chain_rt.sh
#   ./bots/bot1_realtime/execution/run_settlement_chain_rt.sh /chemin/vers/portfolio_rt.sqlite
#   BOT1_PORTFOLIO_RT_SQLITE_PATH=data/bots/realtime/portfolio_rt.sqlite ./bots/bot1_realtime/execution/run_settlement_chain_rt.sh
#
# Défaut base RT : data/bots/realtime/portfolio_rt.sqlite (voir core/bot_rt_data_paths.py).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT_DIR"

mkdir -p "logs"

PYTHON_BIN="${ROOT_DIR}/.venv/bin/python3"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

if [[ "${#}" -ge 1 ]]; then
  _raw="$1"
else
  _raw="${BOT1_PORTFOLIO_RT_SQLITE_PATH:-data/bots/realtime/portfolio_rt.sqlite}"
fi
if [[ "${_raw}" == /* ]]; then
  PORTFOLIO_RT="${_raw}"
else
  PORTFOLIO_RT="${ROOT_DIR}/${_raw}"
fi

echo "[run_settlement_chain_rt] portfolio=${PORTFOLIO_RT}" >&2

"${PYTHON_BIN}" "bots/bot1/execution/settlement_watcher.py" --portfolio-sqlite "${PORTFOLIO_RT}" \
  >> "logs/settlement_watcher_rt.log" 2>&1
"${PYTHON_BIN}" "bots/bot1/execution/exit_engine.py" --portfolio-sqlite "${PORTFOLIO_RT}" \
  >> "logs/exit_engine_rt.log" 2>&1

echo "[run_settlement_chain_rt] terminé (voir logs/settlement_watcher_rt.log, logs/exit_engine_rt.log)" >&2
