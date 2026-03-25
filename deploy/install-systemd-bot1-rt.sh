#!/usr/bin/env bash
# Génère les unités systemd bot 1 temps réel (__REPO_ROOT__ / __RUN_AS_USER__).
# Usage : ./deploy/install-systemd-bot1-rt.sh [REPO_ROOT] [RUN_AS_USER]
set -euo pipefail

REPO_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUN_AS_USER="${2:-${SUDO_USER:-$USER}}"

if [[ ! -d "${REPO_ROOT}/deploy/systemd" ]]; then
  echo "Répertoire deploy/systemd introuvable sous ${REPO_ROOT}" >&2
  exit 1
fi

chmod +x "${REPO_ROOT}/deploy/bot1_rt_daemon.sh" 2>/dev/null || true
chmod +x "${REPO_ROOT}/update_bot_rt.sh" 2>/dev/null || true
chmod +x "${REPO_ROOT}/bots/bot1_realtime/execution/run_settlement_chain_rt.sh" 2>/dev/null || true
chmod +x "${REPO_ROOT}/bots/bot2_realtime/execution/run_settlement_chain_rt.sh" 2>/dev/null || true

_sed_unit() {
  local name="$1"
  sed -e "s|__REPO_ROOT__|${REPO_ROOT}|g" -e "s|__RUN_AS_USER__|${RUN_AS_USER}|g" \
    "${REPO_ROOT}/deploy/systemd/${name}"
}

if [[ "${EUID:-0}" -ne 0 ]]; then
  echo "Exemple (avec sudo) :" >&2
  echo "  sudo REPO_ROOT='${REPO_ROOT}' RUN_AS_USER='${RUN_AS_USER}' $0" >&2
  echo "" >&2
  echo "Sortie sur stdout (à rediriger vers /etc/systemd/system/) :" >&2
  for f in polymarket-bot1-rt.service polymarket-bot1-rt-cycle.service polymarket-bot1-rt-cycle.timer \
    polymarket-bot1-rt-metar.service polymarket-bot1-rt-metar.timer; do
    echo "### ${f}"
    _sed_unit "${f}"
    echo ""
  done
  exit 0
fi

for f in polymarket-bot1-rt.service polymarket-bot1-rt-cycle.service polymarket-bot1-rt-cycle.timer \
  polymarket-bot1-rt-metar.service polymarket-bot1-rt-metar.timer; do
  _sed_unit "${f}" >"/etc/systemd/system/${f}"
  echo "Écrit /etc/systemd/system/${f}"
done

systemctl daemon-reload
echo "OK. Choisir UNE stratégie (ne pas activer daemon + timer cycle + timer metar en double sur le même hôte) :"
echo "  sudo systemctl enable --now polymarket-bot1-rt.service"
echo "  sudo systemctl enable --now polymarket-bot1-rt-cycle.timer"
echo "  sudo systemctl enable --now polymarket-bot1-rt-metar.timer"
