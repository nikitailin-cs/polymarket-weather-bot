#!/usr/bin/env bash
# Boucle adaptative : cadence lente hors fenêtre GFS, cadence rapide en mode alert.
# Utilise noaa_engine.gfs_poll_status() pour choisir l'intervalle.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

SLOW_SEC="${BOT1_CYCLE_INTERVAL_SLEEP_SEC:-600}"
ALERT_SEC="${BOT1_CYCLE_INTERVAL_ALERT_SEC:-25}"
JITTER_SEC="${BOT1_CYCLE_INTERVAL_JITTER_SEC:-3}"
LOG_FILE="${BOT1_DAEMON_LOG:-${ROOT_DIR}/logs/bot1_adaptive_daemon.log}"
mkdir -p logs

_ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

_current_mode() {
  "${ROOT_DIR}/.venv/bin/python3" -c "import pathlib,sys; p=pathlib.Path('${ROOT_DIR}')/'bots'/'bot1'/'Long Term'; sys.path.insert(0,str(p)); import noaa_engine as n; print(n.gfs_poll_mode())" 2>/dev/null \
    || python3 -c "import pathlib,sys; p=pathlib.Path('${ROOT_DIR}')/'bots'/'bot1'/'Long Term'; sys.path.insert(0,str(p)); import noaa_engine as n; print(n.gfs_poll_mode())" 2>/dev/null \
    || echo "sleep"
}

_sleep_for_mode() {
  local mode="$1"
  local base
  if [[ "${mode}" == "alert" ]]; then
    base="${ALERT_SEC}"
  else
    base="${SLOW_SEC}"
  fi
  if [[ "${JITTER_SEC}" -le 0 ]]; then
    echo "${base}"
    return
  fi
  local jitter=$(( RANDOM % (JITTER_SEC + 1) ))
  echo "$(( base + jitter ))"
}

echo "$(_ts) [bot1_adaptive] démarrage repo=${ROOT_DIR} slow=${SLOW_SEC}s alert=${ALERT_SEC}s jitter<=${JITTER_SEC}s log=${LOG_FILE}" | tee -a "${LOG_FILE}"

while true; do
  mode="$(_current_mode)"
  echo "$(_ts) [bot1_adaptive] --- cycle start mode=${mode} ---" | tee -a "${LOG_FILE}"
  # Si le unit systemd n’exporte pas BOT1_NOAA_MIN_RUN_INTERVAL_SEC, update_bot ne peut pas
  # « pré-source » avant .env → un .env à 900 gagne et le manifeste reste skipped_watchdog_interval.
  # Aligner par défaut sur la cadence alert (même défaut que BOT1_CYCLE_INTERVAL_ALERT_SEC).
  export BOT1_NOAA_MIN_RUN_INTERVAL_SEC="${BOT1_NOAA_MIN_RUN_INTERVAL_SEC:-${BOT1_CYCLE_INTERVAL_ALERT_SEC:-25}}"
  set +e
  bash "${ROOT_DIR}/update_bot.sh" >>"${LOG_FILE}" 2>&1
  ec=$?
  set -e
  sleep_sec="$(_sleep_for_mode "${mode}")"
  echo "$(_ts) [bot1_adaptive] --- cycle end exit=${ec} next_sleep=${sleep_sec}s mode=${mode} ---" | tee -a "${LOG_FILE}"
  sleep "${sleep_sec}"
done

