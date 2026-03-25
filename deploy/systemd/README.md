# Systemd — bot1 (NOAA + `finance_engine`)

Deux modes :

| Fichier | Mode |
|---------|------|
| **`polymarket-bot1.service`** | Processus **long** : `deploy/bot1_daemon.sh` boucle avec `sleep` (`BOT1_CYCLE_INTERVAL_SEC`, défaut 900). |
| **`polymarket-bot1-cycle.timer`** + **`polymarket-bot1-cycle.service`** | **Timer systemd** : un cycle `update_bot.sh` toutes les 15 min (sans processus bash `while`). |

Ne pas activer les deux en même temps sur la même machine (double exécution).

## Prérequis

- Repo cloné (ex. `/home/TON_USER/polymarket-weather-bot`)
- `.venv` créé, `pip install -r requirements.txt`
- `.env` à la racine du repo (non géré par systemd : le script utilise le répertoire de travail)

## Installation (script ou sed manuel)

**Remplace le `cd` par la vraie racine du clone** (ex. `cd ~/polymarket-weather-bot`). Ne pas utiliser un chemin d’exemple fictif.

Vérifie que les fichiers existent : `ls deploy/install-systemd-bot1.sh`. S’ils manquent : `git pull` depuis la branche qui contient `deploy/`.

Depuis cette racine :

```bash
cd ~/polymarket-weather-bot   # adapte si ton clone est ailleurs
chmod +x deploy/install-systemd-bot1.sh deploy/bot1_daemon.sh update_bot.sh
sudo ./deploy/install-systemd-bot1.sh
```

Le script écrit `/etc/systemd/system/polymarket-bot1*.service` et `*.timer`, fait `daemon-reload`, et utilise par défaut le chemin du repo + `SUDO_USER` (ou `USER`) pour `User=`.

Sans droits root, le même script affiche sur stdout les unités générées (à copier ou rediriger).

### Remplacer les placeholders à la main

Remplace `__REPO_ROOT__` et `__RUN_AS_USER__` avant installation :

```bash
REPO="$HOME/polymarket-weather-bot"
USER="$(whoami)"

for f in polymarket-bot1.service polymarket-bot1-cycle.service polymarket-bot1-cycle.timer; do
  sed -e "s|__REPO_ROOT__|${REPO}|g" -e "s|__RUN_AS_USER__|${USER}|g" \
    "${REPO}/deploy/systemd/${f}" | sudo tee "/etc/systemd/system/${f}" >/dev/null
done

sudo chmod +x "${REPO}/deploy/bot1_daemon.sh"
sudo chmod +x "${REPO}/update_bot.sh"
```

## Mode A — boucle (`bot1_daemon.sh`)

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-bot1.service
sudo systemctl status polymarket-bot1.service
journalctl -u polymarket-bot1 -f
```

Arrêt :

```bash
sudo systemctl stop polymarket-bot1.service
sudo systemctl disable polymarket-bot1.service
```

## Mode B — timer (cycles disjoints)

Désactiver d’abord le service boucle s’il était actif :

```bash
sudo systemctl disable --now polymarket-bot1.service 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-bot1-cycle.timer
systemctl list-timers --all | grep polymarket
journalctl -u polymarket-bot1-cycle.service -f
```

## Variables d’environnement (unités systemd)

Éditer le fichier sous `/etc/systemd/system/` ou utiliser `systemctl edit polymarket-bot1` (drop-in) :

| Variable | Effet |
|----------|--------|
| `BOT1_CYCLE_INTERVAL_SEC` | Secondes entre deux cycles (**service boucle** uniquement). Défaut 900. |
| `NOMADS_ALLOW_OUTSIDE_WINDOW=1` | Passe `--allow-outside-window` au script NOAA (voir `update_bot.sh`). |
| `NOMADS_FORCE=1` | Passe aussi `--force` au NOAA (ignore l’intervalle min entre runs ; à utiliser avec prudence). |
| `BOT1_DAEMON_LOG` | Chemin absolu du log daemon (défaut : `logs/bot1_daemon.log` dans le repo). |

Après modification : `sudo systemctl daemon-reload && sudo systemctl restart polymarket-bot1.service`.

## Fichiers de log

- `logs/bot1_daemon.log` — sortie agrégée des cycles (mode boucle)
- `logs/finance.log` — append `finance_engine` (comme avant)

## Sécurité

- Le service doit tourner sous un utilisateur **non root** (`User=`).
- Ne commite pas le `.env` ; sur le serveur, droits `chmod 600 .env`.
