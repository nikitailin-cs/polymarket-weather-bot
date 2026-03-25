#!/usr/bin/env bash
# Phase 6 — Orchestration maître **temps réel / scalping METAR** :
#   1) produce_rt_signals.py (ingestion + science binaire)
#   2) Vérification manifeste status == ok
#   3) finance_engine_rt.py (carnet CLOB)
#   4) (optionnel) run_settlement_chain_rt.sh — synchrone par défaut, ou asynchrone si
#      UPDATE_BOT_RT_ASYNC_SETTLEMENT=1
#
# Pipeline par défaut : **bot 2** (``data/bots/bot_2_realtime/``). Ancien bot1 RT :
#   UPDATE_BOT_RT_PIPELINE=bot1 ./update_bot_rt.sh
#
# Journal principal : ``logs/update_bot_rt_cycle.log`` (tout stdout/stderr du script).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

mkdir -p "logs"
LOG_CYCLE="${UPDATE_BOT_RT_CYCLE_LOG:-${ROOT_DIR}/logs/update_bot_rt_cycle.log}"
exec >> "${LOG_CYCLE}" 2>&1

echo "=== [update_bot_rt] start $(date -u +"%Y-%m-%dT%H:%M:%SZ") pipeline=${UPDATE_BOT_RT_PIPELINE:-bot2} ==="

# Export chemins RT (absolus) — bot1_realtime (legacy) ou bot_2_realtime (défaut)
_export_bot_rt_path() {
  local n="$1" d="$2"
  local cur="${!n:-}"
  if [[ -z "${cur}" ]]; then
    export "${n}=${ROOT_DIR}/${d}"
  elif [[ "${cur}" != /* ]]; then
    export "${n}=${ROOT_DIR}/${cur}"
  else
    export "${n}=${cur}"
  fi
}

PIPELINE="${UPDATE_BOT_RT_PIPELINE:-bot2}"

if [[ "${PIPELINE}" == "bot1" ]]; then
  RT_BASE="data/bots/realtime"
  _export_bot_rt_path BOT1_REALTIME_SIGNALS_FILE "${RT_BASE}/realtime_signals.json"
  _export_bot_rt_path BOT_RT_SIGNALS_FILE "${RT_BASE}/realtime_signals.json"
  _export_bot_rt_path BOT1_REALTIME_SIGNALS_META_FILE "${RT_BASE}/realtime_signals_meta.json"
  _export_bot_rt_path BOT_RT_SIGNALS_META_FILE "${RT_BASE}/realtime_signals_meta.json"
  _export_bot_rt_path BOT1_PORTFOLIO_RT_SQLITE_PATH "${RT_BASE}/portfolio_rt.sqlite"
  _export_bot_rt_path BOT_RT_PORTFOLIO_SQLITE_PATH "${RT_BASE}/portfolio_rt.sqlite"
  _export_bot_rt_path BOT1_ICAO_MAPPING_FILE "data/icao_mapping.json"
  PRODUCE_SCRIPT="bots/bot1_realtime/metar_live/produce_rt_signals.py"
  FINANCE_SCRIPT="bots/bot1_realtime/execution/finance_engine_rt.py"
  SETTLEMENT_SCRIPT="${ROOT_DIR}/bots/bot1_realtime/execution/run_settlement_chain_rt.sh"
  META_FILE="${BOT1_REALTIME_SIGNALS_META_FILE}"
else
  RT_BASE="data/bots/bot_2_realtime"
  _export_bot_rt_path BOT2_REALTIME_SIGNALS_FILE "${RT_BASE}/realtime_signals.json"
  _export_bot_rt_path BOT2_REALTIME_SIGNALS_META_FILE "${RT_BASE}/realtime_signals_meta.json"
  _export_bot_rt_path BOT2_PORTFOLIO_RT_SQLITE_PATH "${RT_BASE}/portfolio_rt.sqlite"
  PRODUCE_SCRIPT="bots/bot2_realtime/metar_live/produce_rt_signals.py"
  FINANCE_SCRIPT="bots/bot2_realtime/execution/finance_engine_rt.py"
  SETTLEMENT_SCRIPT="${ROOT_DIR}/bots/bot2_realtime/execution/run_settlement_chain_rt.sh"
  META_FILE="${BOT2_REALTIME_SIGNALS_META_FILE}"
  PORTFOLIO_RT="${BOT2_PORTFOLIO_RT_SQLITE_PATH}"
fi

if [[ "${PIPELINE}" == "bot1" ]]; then
  PORTFOLIO_RT="${BOT1_PORTFOLIO_RT_SQLITE_PATH}"
fi

PYTHON_BIN="${ROOT_DIR}/.venv/bin/python3"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

exit_code=0
set +e
"${PYTHON_BIN}" "${ROOT_DIR}/${PRODUCE_SCRIPT}"
metar_ec=$?
set -e
if [[ "${metar_ec}" -ne 0 ]]; then
  exit_code="${metar_ec}"
  echo "[update_bot_rt] produce_rt_signals terminé avec code ${metar_ec} — finance_engine_rt ignoré" >&2
fi

run_finance=0
fin_ec="skipped"
if [[ "${metar_ec}" -eq 0 ]]; then
  if [[ -f "${META_FILE}" ]]; then
    set +e
    export META_FILE
    "${PYTHON_BIN}" -c "
import json
import os
import sys
from pathlib import Path
p = Path(os.environ['META_FILE'])
m = json.loads(p.read_text(encoding='utf-8'))
sys.exit(0 if str(m.get('status', '')).strip() == 'ok' else 1)
"
    meta_ec=$?
    set -e
    if [[ "${meta_ec}" -eq 0 ]]; then
      run_finance=1
    else
      echo "[update_bot_rt] finance_engine_rt ignoré (meta illisible ou status≠ok)" >&2
    fi
  else
    run_finance=1
    echo "[update_bot_rt] Avertissement: pas de manifeste RT (${META_FILE}) — finance_engine_rt en mode legacy" >&2
  fi
fi

if [[ "${run_finance}" -eq 1 ]]; then
  set +e
  "${PYTHON_BIN}" "${ROOT_DIR}/${FINANCE_SCRIPT}"
  fin_ec=$?
  set -e
  if [[ "${fin_ec}" -ne 0 ]]; then
    exit_code="${fin_ec}"
  fi
fi

chain_ec=0
if [[ "${UPDATE_BOT_RT_SKIP_SETTLEMENT:-0}" != "1" ]]; then
  if [[ "${UPDATE_BOT_RT_ASYNC_SETTLEMENT:-0}" == "1" ]]; then
    (
      bash "${SETTLEMENT_SCRIPT}" "${PORTFOLIO_RT}"
    ) &
    echo "[update_bot_rt] run_settlement_chain_rt lancé en arrière-plan (pid $!)"
  else
    set +e
    bash "${SETTLEMENT_SCRIPT}" "${PORTFOLIO_RT}"
    chain_ec=$?
    set -e
    if [[ "${chain_ec}" -ne 0 ]]; then
      exit_code="${chain_ec}"
    fi
  fi
else
  echo "[update_bot_rt] settlement ignoré (UPDATE_BOT_RT_SKIP_SETTLEMENT=1)"
fi

_ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
if [[ "${fin_ec}" == "skipped" ]]; then
  _fin_json="null"
  _fin_skipped="true"
else
  _fin_json="${fin_ec}"
  _fin_skipped="false"
fi
_line="{\"ts\":\"${_ts}\",\"pipeline\":\"${PIPELINE}\",\"step_exit_codes\":{\"metar\":${metar_ec},\"finance_rt\":${_fin_json},\"finance_rt_skipped\":${_fin_skipped},\"settlement_chain\":${chain_ec}},\"aggregated_exit_code\":${exit_code}}"
echo "[update_bot_rt] cycle_summary ${_line}" >&2
echo "${_line}"

exit "${exit_code}"
