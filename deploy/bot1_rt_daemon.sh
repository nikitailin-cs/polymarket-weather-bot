#!/usr/bin/env bash
# Boucle : enchaîne update_bot_rt.sh (METAR + finance_rt + settlement) puis dort BOT1_RT_CYCLE_INTERVAL_SEC.
# Défaut 300 s (5 min). Ex. systemd : polymarket-bot1-rt.service
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

INTERVAL_SEC="${BOT1_RT_CYCLE_INTERVAL_SEC:-300}"
mkdir -p logs
LOG_FILE="${BOT1_RT_DAEMON_LOG:-${ROOT_DIR}/logs/bot1_rt_daemon.log}"

_ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

echo "$(_ts) [bot1_rt_daemon] démarrage repo=${ROOT_DIR} intervalle=${INTERVAL_SEC}s log=${LOG_FILE}" | tee -a "${LOG_FILE}"

while true; do
  echo "$(_ts) [bot1_rt_daemon] --- cycle start ---" | tee -a "${LOG_FILE}"
  set +e
  UPDATE_BOT_RT_PIPELINE=bot1 bash "${ROOT_DIR}/update_bot_rt.sh" >>"${LOG_FILE}" 2>&1
  ec=$?
  set -e
  echo "$(_ts) [bot1_rt_daemon] --- cycle end exit=${ec} ---" | tee -a "${LOG_FILE}"
  sleep "${INTERVAL_SEC}"
done
