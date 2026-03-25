# Systemd — bot1 (NOAA + finance + settlement)

## Chaîne métier (une vérité)

Chaque cycle **`update_bot.sh`** exécute **dans cet ordre** :

1. **NOAA** (`produce_trading_signals.py`, alias `run_nomads_live_test.py`) → signaux + manifeste (chemins : `TRADING_SIGNALS_FILE` / `TRADING_SIGNALS_META_FILE`, défaut `data/…`)  
2. **`finance_engine.py`** → positions **OPEN** dans `PORTFOLIO_SQLITE_PATH` (défaut `data/portfolio.sqlite`)  
3. **`settlement_watcher.py`** → **OPEN → RESOLVED** (API Gamma : marché résolu / clos)  
4. **`exit_engine.py`** → **RESOLVED → SETTLED** (redeem on-chain, PnL, Telegram)

Sans l’étape 3, `exit_engine` ne voit aucune ligne **RESOLVED** : le watcher est donc obligatoire dans le déploiement standard (désormais **inclus** dans `update_bot.sh`).

Les appels sont **idempotents** : plusieurs passages watcher / exit_engine restent sûrs (statuts SQLite + table `redeem_attempts`).

## Modes principaux

| Fichier | Mode |
|---------|------|
| **`polymarket-bot1.service`** | Processus **long** : `deploy/bot1_daemon.sh` boucle avec `sleep` (`BOT1_CYCLE_INTERVAL_SEC`, défaut 900). |
| **`polymarket-bot1-adaptive.service`** | Processus **long adaptatif** : `deploy/bot1_adaptive_daemon.sh` cadence lente hors fenêtre GFS et cadence rapide en mode `alert`. |
| **`polymarket-bot1-cycle.timer`** + **`polymarket-bot1-cycle.service`** | **Timer systemd** : un cycle `update_bot.sh` toutes les 15 min (sans processus bash `while`). |

Ne pas activer en parallèle plusieurs modes (`polymarket-bot1.service`, `polymarket-bot1-adaptive.service`, `polymarket-bot1-cycle.timer`) sur la même machine (double exécution des cycles).

## Option — settlement seul (sans NOAA/finance)

Si sur une machine tu lances **NOAA + finance ailleurs** (autre hôte, manuel, etc.) mais tu veux quand même faire tourner **watcher + exit** sur un rythme propre :

| Fichier | Rôle |
|---------|------|
| **`polymarket-bot1-settlement-chain.timer`** + **`polymarket-bot1-settlement-chain.service`** | Oneshot : `bots/bot1/execution/run_settlement_chain.sh` (watcher puis exit). Défaut timer : **1 h**. |

**Ne pas** activer ce timer sur un serveur qui exécute déjà `update_bot.sh` en boucle ou via `polymarket-bot1-cycle.timer` : tu **doublerais** inutilement la chaîne settlement (reste sûr mais bruyant dans les logs).

```bash
sudo systemctl enable --now polymarket-bot1-settlement-chain.timer
journalctl -u polymarket-bot1-settlement-chain.service -f
```

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

for f in polymarket-bot1.service polymarket-bot1-cycle.service polymarket-bot1-cycle.timer \
  polymarket-bot1-adaptive.service \
  polymarket-bot1-settlement-chain.service polymarket-bot1-settlement-chain.timer; do
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

## Mode C — daemon adaptatif (slow + burst)

Ce mode est recommandé pour réagir plus vite aux fenêtres de publication GFS sans marteler NOAA en continu.

```bash
sudo systemctl disable --now polymarket-bot1.service polymarket-bot1-cycle.timer 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-bot1-adaptive.service
sudo systemctl status polymarket-bot1-adaptive.service
journalctl -u polymarket-bot1-adaptive.service -f
```

## Variables d’environnement (unités systemd)

Éditer le fichier sous `/etc/systemd/system/` ou utiliser `systemctl edit polymarket-bot1` (drop-in) :

| Variable | Effet |
|----------|--------|
| `BOT1_CYCLE_INTERVAL_SEC` | Secondes entre deux cycles (**service boucle** uniquement). Défaut 900. |
| `BOT1_CYCLE_INTERVAL_SLEEP_SEC` | Intervalle hors fenêtre GFS pour `polymarket-bot1-adaptive.service` (défaut 600). |
| `BOT1_CYCLE_INTERVAL_ALERT_SEC` | Intervalle en fenêtre `alert` pour `polymarket-bot1-adaptive.service` (défaut 25). |
| `BOT1_CYCLE_INTERVAL_JITTER_SEC` | Jitter aléatoire ajouté à chaque pause adaptative (défaut 3). |
| `NOMADS_ALLOW_OUTSIDE_WINDOW=1` | Passe `--allow-outside-window` au script NOAA (voir `update_bot.sh`). |
| `NOMADS_FORCE=1` | Passe aussi `--force` au NOAA (ignore l’intervalle min entre runs ; à utiliser avec prudence). |
| `BOT1_REFRESH_ACTIVE_MARKETS_FROM_GAMMA=1` | Avant NOAA, régénère `data/active_markets.json` via scanner live (`generate_markets.py`). |
| `BOT1_MARKET_SCAN_LIMIT` | Limite Gamma API pour la régénération (défaut 500). |
| `BOT1_FAIL_ON_MARKET_SCAN_ERROR=1` | Si le scan live échoue, stoppe le cycle complet (sinon continue avec registre précédent). |
| `BOT1_DAEMON_LOG` | Chemin absolu du log daemon (défaut : `logs/bot1_daemon.log` dans le repo). |
| `UPDATE_BOT_CYCLE_SUMMARY_LOG` | Fichier du résumé JSON par cycle (défaut : `logs/update_bot_cycle.log`). |

### Fichiers bot1 (NOAA / finance / settlement)

Chemins absolus ou relatifs à la racine du repo ; `update_bot.sh` les normalise en absolu. Détail : `core/bot1_data_paths.py`. Exemple compartiment : `python main_compartment.py --bot 1` préremplit `data/bots/bot_1/…`.

| Variable | Défaut |
|----------|--------|
| `TRADING_SIGNALS_FILE` | `data/trading_signals.json` |
| `TRADING_SIGNALS_META_FILE` | `data/trading_signals_meta.json` |
| `PORTFOLIO_SQLITE_PATH` | `data/portfolio.sqlite` |
| `NOAA_WATCHDOG_STATE_PATH` | `data/logs/noaa_watchdog_state.json` |
| `LAST_NOAA_RUN_FILE` | `data/last_noaa_run.json` |

### `finance_engine` (signaux NOAA)

| Variable | Effet |
|----------|--------|
| `TRADING_SIGNALS_MAX_AGE_SEC` | Défaut **21600** (6 h). Si `generated_at_utc` du manifeste est plus vieux, pas d’ordres. Mettre **-1** pour désactiver la limite d’âge. |
| `TRADING_SIGNALS_REQUIRE_SAME_UTC_DATE` | Si `1` / `true` : exige que la date UTC de `generated_at_utc` soit celle du jour (stricte). |
| `DRY_RUN` | Défaut `1` : pas d’ordres réels côté `finance_engine` (sauf si `FINANCE_ENGINE_DRY_RUN=0`). |
| `FINANCE_ENGINE_DRY_RUN` | `1`/`0` : force le dry-run ou le live **uniquement** pour `finance_engine` ; si absent, suit `DRY_RUN`. |

### HTTP Gamma / CLOB (bot1)

`finance_engine`, `settlement_watcher` et `exit_engine` utilisent `core/bot1_http.py` : **même** proxy SOCKS que le CLOB (`POLYMARKET_PROXY_ENABLED=1` + `POLYMARKET_SOCKS5_*`). Dépend de **PySocks** (déjà requis pour `requests` via SOCKS).

| Variable | Effet |
|----------|--------|
| `BOT1_HTTP_TIMEOUT_SEC` | Timeout GET (défaut **20**). Alias accepté : `GAMMA_HTTP_TIMEOUT_SEC`. |
| `BOT1_HTTP_USER_AGENT` | User-Agent des GET (défaut `POLYMARKET-bot1-http/1.0`). |

Après modification : `sudo systemctl daemon-reload && sudo systemctl restart polymarket-bot1.service`.

## Fichiers de log

- `logs/bot1_daemon.log` — sortie agrégée des cycles (mode boucle)
- `logs/finance.log` — `finance_engine`
- `logs/settlement_watcher.log` — `settlement_watcher` (chaîne OPEN → RESOLVED)
- `logs/exit_engine.log` — `exit_engine` (redeem, RESOLVED → SETTLED)
- `logs/update_bot_cycle.log` — une ligne JSON par cycle (`step_exit_codes`, `aggregated_exit_code`)
- `logs/gamma_scan.log` — sortie de la régénération scanner live -> `active_markets.json`
- `logs/update_bot_rt_cycle.log` — sortie agrégée du script `update_bot_rt.sh` (Phase 6, bot RT)

## Bot 1 temps réel (METAR, `update_bot_rt.sh`)

Chaîne parallèle au batch NOAA : `produce_rt_signals.py` → `finance_engine_rt.py` → settlement sur `portfolio_rt.sqlite`. Unités : `deploy/install-systemd-bot1-rt.sh`, templates `polymarket-bot1-rt*.service` / `*.timer`.

| Fichier | Rôle |
|---------|------|
| **`polymarket-bot1-rt.service`** | Boucle : `deploy/bot1_rt_daemon.sh` (`BOT1_RT_CYCLE_INTERVAL_SEC`, défaut 300 s). |
| **`polymarket-bot1-rt-cycle.timer`** + **`polymarket-bot1-rt-cycle.service`** | Un cycle `update_bot_rt.sh` à intervalle fixe (ex. toutes les 5 min). |
| **`polymarket-bot1-rt-metar.timer`** + **`polymarket-bot1-rt-metar.service`** | Cycles aux minutes **:52, :55, :58** (horloge **du serveur**). |

Ne pas activer en parallèle plusieurs modes sur le même hôte (double exécution des cycles RT).

### Timer METAR et fuseau du serveur

Les `OnCalendar` systemd utilisent l’**horloge système** du serveur (souvent **UTC** en production). Les déclenchements **:52, :55, :58** sont donc ces minutes-là **sur cette horloge** — pas automatiquement « l’heure locale » d’un aéroport donné. En pratique, une grille serrée plusieurs fois par heure permet quand même de rafraîchir souvent après les passages METAR typiques ; pour viser un fuseau précis (ex. États-Unis), soit le serveur est réglé / pensé dans ce contexte, soit on ajuste les `OnCalendar` (équivalents UTC) ou on s’appuie sur le timer **intervalle** (`polymarket-bot1-rt-cycle.timer`).

### Wallet proxy et liquidité (batch + temps réel)

Le batch NOAA (`update_bot.sh` / `finance_engine.py`) et le temps réel (`update_bot_rt.sh` / `finance_engine_rt.py`) utilisent en général le **même** compte Polymarket (même clé / **même funder proxy**) → le même **USDC** disponible pour passer des ordres.

**Risque partagé** : si le batch a fortement engagé les fonds le matin (prévisions GFS), le scalping RT l’après-midi peut manquer d’USDC pour des opportunités « sûres ».

**Mesure opérationnelle** : maintenir une **réserve de liquidité** (non allouée au batch, ou plafonds distincts suivis à la main) pour que les deux pipelines ne se fient pas au même solde « zéro marge ».

### Phase 6 — `polymarket-bot-rt` (scalping METAR, bot2 par défaut)

| Fichier | Rôle |
|---------|------|
| **`polymarket-bot-rt.timer`** + **`polymarket-bot-rt.service`** | Un cycle `update_bot_rt.sh` aux minutes **:51, :53, :55, :57, :59** (horloge **du serveur**). |
| **`deploy/install-systemd-bot-rt.sh`** | Installe les unités (2ᵉ argument = utilisateur Unix, ex. `social_content_check`). |

`update_bot_rt.sh` enchaîne par défaut le pipeline **bot 2** (`bots/bot2_realtime/…`, données sous `data/bots/bot_2_realtime/`). Pour l’ancien bot1 RT : `UPDATE_BOT_RT_PIPELINE=bot1`.

- **Journal principal** : `logs/update_bot_rt_cycle.log` (stdout/stderr du script).
- **Settlement** : synchrone par défaut ; `UPDATE_BOT_RT_ASYNC_SETTLEMENT=1` pour lancer `run_settlement_chain_rt.sh` en arrière-plan ; `UPDATE_BOT_RT_SKIP_SETTLEMENT=1` pour le désactiver.
- **Rotation logs** : `deploy/logrotate/polymarket-bot-rt.conf` (adapter le chemin absolu, puis copier vers `/etc/logrotate.d/`).
- **Dry-run 24 h avant prod** : `deploy/systemd/PHASE6_RT_DRY_RUN.md`.

```bash
chmod +x deploy/install-systemd-bot-rt.sh update_bot_rt.sh
sudo ./deploy/install-systemd-bot-rt.sh "$HOME/polymarket-weather-bot" social_content_check
sudo systemctl enable --now polymarket-bot-rt.timer
sudo systemctl list-timers polymarket-bot-rt.timer
```

## Sécurité

- Le service doit tourner sous un utilisateur **non root** (`User=`).
- Ne commite pas le `.env` ; sur le serveur, droits `chmod 600 .env`.
