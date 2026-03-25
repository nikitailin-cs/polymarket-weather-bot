#!/usr/bin/env bash
# Phase 5 — Chaîne settlement **bot 2 temps réel** (``portfolio_rt.sqlite`` isolé) :
#   1) ``settlement_watcher.py`` : OPEN → RESOLVED (API Gamma)
#   2) pause (défaut quelques secondes, configurable)
#   3) ``exit_engine.py`` : RESOLVED → redeem on-chain → SETTLED
#
# Active ``.venv`` si présent, puis enchaîne les deux scripts Python avec le même
# ``--portfolio-sqlite``.
#
# Usage :
#   bash bots/bot2_realtime/execution/run_settlement_chain_rt.sh
#   bash bots/bot2_realtime/execution/run_settlement_chain_rt.sh /chemin/absolu/portfolio_rt.sqlite
#   BOT2_PORTFOLIO_RT_SQLITE_PATH=data/bots/bot_2_realtime/portfolio_rt.sqlite \\
#     SETTLEMENT_CHAIN_RT_SLEEP_SEC=10 bash bots/bot2_realtime/execution/run_settlement_chain_rt.sh
#
# Défaut base : ``data/bots/bot_2_realtime/portfolio_rt.sqlite`` (voir ``core/bot2_data_paths.py``).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT_DIR"

mkdir -p "${ROOT_DIR}/logs"

if [[ -f "${ROOT_DIR}/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.venv/bin/activate"
fi

PYTHON_BIN="${ROOT_DIR}/.venv/bin/python3"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
fi
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "[run_settlement_chain_rt bot2] erreur: python3 introuvable (activez .venv ou installez Python)." >&2
  exit 1
fi

if [[ "${#}" -ge 1 ]]; then
  _raw="$1"
else
  _raw="${BOT2_PORTFOLIO_RT_SQLITE_PATH:-data/bots/bot_2_realtime/portfolio_rt.sqlite}"
fi
if [[ "${_raw}" == /* ]]; then
  PORTFOLIO_RT="${_raw}"
else
  PORTFOLIO_RT="${ROOT_DIR}/${_raw}"
fi

SLEEP_SEC="${SETTLEMENT_CHAIN_RT_SLEEP_SEC:-8}"

echo "[run_settlement_chain_rt bot2] venv=${VIRTUAL_ENV:-non activé} portfolio=${PORTFOLIO_RT} sleep=${SLEEP_SEC}s" >&2

"${PYTHON_BIN}" "${ROOT_DIR}/bots/bot1/execution/settlement_watcher.py" --portfolio-sqlite "${PORTFOLIO_RT}" \
  >> "${ROOT_DIR}/logs/settlement_watcher_bot2_rt.log" 2>&1

sleep "${SLEEP_SEC}"

"${PYTHON_BIN}" "${ROOT_DIR}/bots/bot1/execution/exit_engine.py" --portfolio-sqlite "${PORTFOLIO_RT}" \
  >> "${ROOT_DIR}/logs/exit_engine_bot2_rt.log" 2>&1

echo "[run_settlement_chain_rt bot2] terminé (logs/settlement_watcher_bot2_rt.log, logs/exit_engine_bot2_rt.log)" >&2
