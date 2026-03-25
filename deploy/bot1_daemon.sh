#!/usr/bin/env bash
# Boucle h24 : enchaîne update_bot.sh (NOAA + finance_engine) puis dort BOT1_CYCLE_INTERVAL_SEC.
# Lancer via systemd (polymarket-bot1.service) ou manuellement : ./deploy/bot1_daemon.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

INTERVAL_SEC="${BOT1_CYCLE_INTERVAL_SEC:-900}"
mkdir -p logs
LOG_FILE="${BOT1_DAEMON_LOG:-${ROOT_DIR}/logs/bot1_daemon.log}"

_ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

echo "$(_ts) [bot1_daemon] démarrage repo=${ROOT_DIR} intervalle=${INTERVAL_SEC}s log=${LOG_FILE}" | tee -a "${LOG_FILE}"

while true; do
  echo "$(_ts) [bot1_daemon] --- cycle start ---" | tee -a "${LOG_FILE}"
  set +e
  bash "${ROOT_DIR}/update_bot.sh" >>"${LOG_FILE}" 2>&1
  ec=$?
  set -e
  echo "$(_ts) [bot1_daemon] --- cycle end exit=${ec} ---" | tee -a "${LOG_FILE}"
  sleep "${INTERVAL_SEC}"
done
