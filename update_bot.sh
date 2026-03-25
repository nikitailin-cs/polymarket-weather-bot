#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

mkdir -p "logs" "data/logs"
LOCKFILE="${ROOT_DIR}/data/logs/update_bot.lock"
exec 200>"${LOCKFILE}"
if ! flock -n 200; then
  echo "[update_bot] déjà en cours (${LOCKFILE}), skip" >&2
  exit 0
fi

# Export chemins bot1 (absolus) pour tous les Python du cycle — voir core/bot1_data_paths.py
_export_bot1_path() {
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
_export_bot1_path TRADING_SIGNALS_FILE "data/trading_signals.json"
_export_bot1_path TRADING_SIGNALS_META_FILE "data/trading_signals_meta.json"
_export_bot1_path PORTFOLIO_SQLITE_PATH "data/portfolio.sqlite"
_export_bot1_path NOAA_WATCHDOG_STATE_PATH "data/logs/noaa_watchdog_state.json"
_export_bot1_path LAST_NOAA_RUN_FILE "data/last_noaa_run.json"

PYTHON_BIN="${ROOT_DIR}/.venv/bin/python3"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

BOT1_ACTIVE_MARKETS_IDS_STATE="${ROOT_DIR}/data/logs/bot1_active_markets_ids.json"
BOT1_ACTIVE_MARKETS_JSON="${ROOT_DIR}/data/active_markets.json"

exit_code=0
scan_ec="skipped"
scan_skipped=true
if [[ "${BOT1_REFRESH_ACTIVE_MARKETS_FROM_GAMMA:-1}" == "1" ]]; then
  scan_skipped=false
  SCAN_LIMIT="${BOT1_MARKET_SCAN_LIMIT:-500}"
  set +e
  "${PYTHON_BIN}" "bots/bot1/Long Term/scripts/generate_markets.py" --limit "${SCAN_LIMIT}" >> "logs/gamma_scan.log" 2>&1
  scan_ec=$?
  set -e
  if [[ "${scan_ec}" -ne 0 ]]; then
    echo "[update_bot] génération active_markets depuis scanner live échouée (code=${scan_ec})" >&2
    if [[ "${BOT1_FAIL_ON_MARKET_SCAN_ERROR:-0}" == "1" ]]; then
      exit_code="${scan_ec}"
      _ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
      SUMMARY_LOG="${UPDATE_BOT_CYCLE_SUMMARY_LOG:-${ROOT_DIR}/logs/update_bot_cycle.log}"
      _line="{\"ts\":\"${_ts}\",\"step_exit_codes\":{\"market_scan\":${scan_ec},\"market_scan_skipped\":false,\"noaa\":null,\"finance\":null,\"finance_skipped\":true,\"settlement_watcher\":null,\"exit_engine\":null},\"aggregated_exit_code\":${exit_code},\"reason\":\"market_scan_failed_and_fail_on_error\"}"
      echo "${_line}" >> "${SUMMARY_LOG}"
      echo "[update_bot] cycle_summary ${_line}" >&2
      exit "${exit_code}"
    fi
  fi
fi

NOMADS_EXTRA=()
if [[ "${NOMADS_ALLOW_OUTSIDE_WINDOW:-0}" == "1" ]]; then
  NOMADS_EXTRA+=(--allow-outside-window)
fi
if [[ "${NOMADS_FORCE:-0}" == "1" ]]; then
  NOMADS_EXTRA+=(--force)
fi

BOT1_ADDED_MARKETS_COUNT=0
BOT1_BOOTSTRAP=0
_diff_ok=0
if [[ "${scan_skipped}" == "false" && "${scan_ec}" -eq 0 ]]; then
  set +e
  _diff_out=$("${PYTHON_BIN}" "monitoring/bot1_active_markets_state.py" compute \
    --active "${BOT1_ACTIVE_MARKETS_JSON}" \
    --state "${BOT1_ACTIVE_MARKETS_IDS_STATE}")
  _diff_ec=$?
  set -e
  if [[ "${_diff_ec}" -eq 0 ]]; then
    eval "${_diff_out}"
    _diff_ok=1
  else
    echo "[update_bot] bot1_active_markets_state compute échoué (code=${_diff_ec})" >&2
  fi
  if [[ "${BOT1_ADDED_MARKETS_COUNT}" -gt 0 && "${BOT1_BOOTSTRAP}" -eq 0 ]]; then
    set +e
    "${PYTHON_BIN}" "bots/bot1/Long Term/scripts/sync_rt_mapping_from_active_markets.py" >> "logs/sync_rt_mapping.log" 2>&1
    set -e
    _min_iv="${BOT1_NEW_MARKETS_MIN_RUN_INTERVAL_SEC:-60}"
    _has_allow_outside=0
    for _x in "${NOMADS_EXTRA[@]}"; do
      if [[ "${_x}" == "--allow-outside-window" ]]; then
        _has_allow_outside=1
        break
      fi
    done
    if [[ "${_has_allow_outside}" -eq 0 ]]; then
      NOMADS_EXTRA+=(--allow-outside-window)
    fi
    NOMADS_EXTRA+=(--min-run-interval-sec "${_min_iv}")
  fi
fi

set +e
"${PYTHON_BIN}" "bots/bot1/noaa_nomads_live/produce_trading_signals.py" "${NOMADS_EXTRA[@]}"
noaa_ec=$?
set -e
if [[ "${noaa_ec}" -ne 0 ]]; then
  exit_code="${noaa_ec}"
  echo "[update_bot] NOAA terminé avec code ${noaa_ec} — finance_engine ignoré (pas de trade sur échec NOAA)" >&2
fi

# Persistance de l'ensemble courant : bootstrap, pas de nouveaux marchés, ou NOAA OK après nouveaux marchés.
if [[ "${scan_skipped}" == "false" && "${scan_ec}" -eq 0 && "${_diff_ok}" -eq 1 ]]; then
  _persist=0
  if [[ "${BOT1_BOOTSTRAP}" -eq 1 ]]; then
    _persist=1
  elif [[ "${BOT1_ADDED_MARKETS_COUNT}" -eq 0 ]]; then
    _persist=1
  elif [[ "${noaa_ec}" -eq 0 ]]; then
    _persist=1
  fi
  if [[ "${_persist}" -eq 1 ]]; then
    set +e
    "${PYTHON_BIN}" "monitoring/bot1_active_markets_state.py" persist \
      --active "${BOT1_ACTIVE_MARKETS_JSON}" \
      --state "${BOT1_ACTIVE_MARKETS_IDS_STATE}"
    _persist_ec=$?
    set -e
    if [[ "${_persist_ec}" -ne 0 ]]; then
      echo "[update_bot] avertissement: persistance ${BOT1_ACTIVE_MARKETS_IDS_STATE} échouée (code=${_persist_ec})" >&2
    fi
  fi
fi

run_finance=0
fin_ec="skipped"
if [[ "${noaa_ec}" -eq 0 ]]; then
  if [[ -f "${TRADING_SIGNALS_META_FILE}" ]]; then
    set +e
    "${PYTHON_BIN}" -c "
import json
import os
import sys
from pathlib import Path
p = Path(os.environ['TRADING_SIGNALS_META_FILE'])
m = json.loads(p.read_text(encoding='utf-8'))
sys.exit(0 if str(m.get('status', '')).strip() == 'ok' else 1)
"
    meta_ec=$?
    set -e
    if [[ "${meta_ec}" -eq 0 ]]; then
      run_finance=1
    else
      echo "[update_bot] finance_engine ignoré (meta NOAA illisible ou status≠ok)" >&2
    fi
  else
    run_finance=1
    echo "[update_bot] Avertissement: pas de manifeste NOAA (${TRADING_SIGNALS_META_FILE}) — finance_engine en mode legacy" >&2
  fi
fi

if [[ "${run_finance}" -eq 1 ]]; then
  set +e
  "${PYTHON_BIN}" "bots/bot1/execution/finance_engine.py" >> "logs/finance.log" 2>&1
  fin_ec=$?
  set -e
  if [[ "${fin_ec}" -ne 0 ]]; then
    exit_code="${fin_ec}"
  fi
fi

# Chaîne settlement : OPEN -> RESOLVED (Gamma) puis RESOLVED -> SETTLED (redeem).
# Ordre fixe : watcher puis exit_engine (exit ne traite que les lignes RESOLVED).
set +e
"${PYTHON_BIN}" "bots/bot1/execution/settlement_watcher.py" >> "logs/settlement_watcher.log" 2>&1
watch_ec=$?
set -e
if [[ "${watch_ec}" -ne 0 ]]; then
  exit_code="${watch_ec}"
fi

set +e
"${PYTHON_BIN}" "bots/bot1/execution/exit_engine.py" >> "logs/exit_engine.log" 2>&1
exit_ec=$?
set -e
if [[ "${exit_ec}" -ne 0 ]]; then
  exit_code="${exit_ec}"
fi

SUMMARY_LOG="${UPDATE_BOT_CYCLE_SUMMARY_LOG:-${ROOT_DIR}/logs/update_bot_cycle.log}"
_ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
if [[ "${fin_ec}" == "skipped" ]]; then
  _fin_json="null"
  _fin_skipped="true"
else
  _fin_json="${fin_ec}"
  _fin_skipped="false"
fi
if [[ "${scan_skipped}" == "true" ]]; then
  _scan_json="null"
else
  _scan_json="${scan_ec}"
fi
_line="{\"ts\":\"${_ts}\",\"step_exit_codes\":{\"market_scan\":${_scan_json},\"market_scan_skipped\":${scan_skipped},\"noaa\":${noaa_ec},\"finance\":${_fin_json},\"finance_skipped\":${_fin_skipped},\"settlement_watcher\":${watch_ec},\"exit_engine\":${exit_ec}},\"aggregated_exit_code\":${exit_code},\"bot1_market_diff\":{\"added_count\":${BOT1_ADDED_MARKETS_COUNT:-0},\"bootstrap\":${BOT1_BOOTSTRAP:-0}}}"
echo "${_line}" >> "${SUMMARY_LOG}"
echo "[update_bot] cycle_summary ${_line}" >&2

exit "${exit_code}"
