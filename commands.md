# Commandes serveur (référence rapide)

Chemins type : `~/polymarket-weather-bot`. Préfixer par `cd` + activer le venv si besoin.

| Commande | Rôle (bref) |
|----------|----------------|
| `cd ~/polymarket-weather-bot` | Va dossier projet |
| `source .venv/bin/activate` | Active interpréteur Python projet |
| `git pull` | Met à jour code distant |
| `pip install -r requirements.txt` | Installe dépendances Python |

## Après `git pull` : restart Bot long terme + Bot RT + vérif logs (un bloc)

`git pull` ne recharge pas le code déjà en mémoire. Redémarrer les unités systemd, puis lire tout de suite les logs / health.

- **Bot long terme** : `polymarket-bot1-adaptive.service`
- **Bot temps réel / scalping** : `polymarket-bot-rt.timer` (déclenche `polymarket-bot-rt.service` en oneshot)

**Première installation du timer RT** (une seule fois sur la machine) : voir `deploy/systemd/README.md` ou  
`chmod +x deploy/install-systemd-bot-rt.sh update_bot_rt.sh` puis  
`sudo ./deploy/install-systemd-bot-rt.sh "$HOME/polymarket-weather-bot" VOTRE_USER` puis  
`sudo systemctl enable --now polymarket-bot-rt.timer`.

```bash
cd ~/polymarket-weather-bot
git pull
sudo systemctl daemon-reload
sudo systemctl restart polymarket-bot1-adaptive.service
sudo systemctl restart polymarket-bot-rt.timer

echo "=== systemd (doit afficher active) ==="
systemctl is-active polymarket-bot1-adaptive.service
systemctl is-active polymarket-bot-rt.timer
systemctl status polymarket-bot1-adaptive.service --no-pager -l | head -n 15
systemctl status polymarket-bot-rt.timer --no-pager -l | head -n 12

echo "=== Bot1 — derniers cycles (fiable) ==="
tail -n 30 logs/bot1_adaptive_daemon.log
tail -n 8 logs/update_bot_cycle.log

echo "=== Bot2 RT — santé fichiers signaux ==="
source .venv/bin/activate
python bots/bot2_realtime/scripts/rt_health_check.py

echo "=== Bot2 RT — dernier run service (journald, toujours dispo) ==="
journalctl -u polymarket-bot-rt.service -n 50 --no-pager

echo "=== Bot2 RT — fichier log optionnel ==="
test -f logs/update_bot_rt.log && tail -n 40 logs/update_bot_rt.log || echo "(pas de logs/update_bot_rt.log — normal si seul journalctl est utilisé)"

echo "=== CLOB / wallet (sanity) ==="
python bots/bot1/execution/status_live.py
```

Interprétation rapide : Bot1 OK si le `tail` de `bot1_adaptive_daemon.log` montre des lignes récentes `cycle start` / `cycle end` sans `ModuleNotFoundError`. Bot2 OK si `rt_health_check` ne commence pas par `ERROR` et l’âge du meta reste sous le TTL affiché. Pas d’ordre Polymarket garanti à chaque cycle : absence de trade peut être normale (filtres / fenêtres).

**Bot2 RT manuel** (debug ponctuel, pas prod continue) :

```bash
python bots/bot2_realtime/metar_live/produce_rt_signals.py
python bots/bot2_realtime/scripts/rt_health_check.py
python bots/bot2_realtime/execution/finance_engine_rt.py
```

## Telegram — commandes interactives (rapport live)

Un second processus écoute **Telegram en long polling** et répond aux commandes `/…` (même `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID` que les alertes). **Seul** le chat `TELEGRAM_CHAT_ID` est autorisé.

Lancer sur le serveur (tmux, screen, ou service systemd dédié) :

```bash
cd ~/polymarket-weather-bot
source .venv/bin/activate
python monitoring/telegram_command_bot.py
```

Test rapide (un seul poll puis quitte) :

```bash
python monitoring/telegram_command_bot.py --once
```

| Commande | Contenu |
|----------|---------|
| `/help` | Liste des commandes |
| `/ping` | Test |
| `/health` ou `/status` | `systemctl` bot1-adaptive + timer RT, santé Bot2, extrait meta NOAA |
| `/lastcycle` | Fin de `logs/update_bot_cycle.log` |
| `/daemon` | Fin de `logs/bot1_adaptive_daemon.log` |
| `/bot1meta` | `trading_signals_meta.json` |
| `/bot2meta` | `realtime_signals_meta.json` (Bot2) |
| `/positions` | Derniers trades SQLite bot1 (`executed_trades`) |
| `/positions2` | Dernières lignes `positions` bot2 RT |
| `/rt` | Santé signaux temps réel (âge + health) |
| `/markets` | Scan Gamma météo (peut prendre ~10–30 s, charge API) |

## Fichier `.env` (secrets, proxy, clés)

Depuis la racine du repo (`cd ~/polymarket-weather-bot`) :

| Commande | Rôle (bref) |
|----------|----------------|
| `nano .env` | Ouvre éditeur fichier config |
| `nano ~/polymarket-weather-bot/.env` | Même chose chemin absolu |
| `less .env` | Lit sans modifier |
| `cat .env` | Affiche tout (éviter partage écran) |

Sauvegarde sous **nano** : `Ctrl+O`, Entrée, puis `Ctrl+X` pour quitter.

## Bot & opportunités

### Bot1 en production : `update_bot.sh` (pas `main.py`)

Pour le pipeline **NOAA → signaux → finance → settlement** (bot1), la commande à utiliser est **`bash update_bot.sh`**. Ce n’est **pas** la même chose que `python main.py` (autre boucle : scanner marchés, WebSocket, `edge_detector`, voir ci-dessous).

- **Un lancement** de `update_bot.sh` = **un cycle** complet (puis le script se termine).
- Pour que le bot **tourne en continu**, il faut **relancer** ce script sur un rythme régulier : **cron** sur le serveur ou **timer systemd** (voir `deploy/systemd/README.md`). Exemple manuel répété : `while true; do bash update_bot.sh; sleep 3600; done` (à adapter : intervalle, logs, gestion d’erreur — en prod préférer cron/systemd).

| Commande | Rôle (bref) |
|----------|----------------|
| `python main.py` | Autre bot : boucle trading **continue** (scanner météo, WS prix, edges, ordres) — **ne remplace pas** le cycle bot1 NOAA/finance ci-dessous |
| `python signals/edge_detector.py` | Affiche opportunités ponctuelles |
| `bash update_bot.sh` | **Bot1 — cycle à lancer** : NOAA (`produce_trading_signals.py`) → `finance_engine` → `settlement_watcher` (OPEN→RESOLVED) → `exit_engine` (redeem, SETTLED, Telegram) ; résumé JSON par cycle dans `logs/update_bot_cycle.log` |
| `sudo systemctl enable --now polymarket-bot1-adaptive.service` | **Bot1 adaptatif** : cadence lente hors fenêtre GFS, cadence rapide en `alert` |
| `python bots/bot1/noaa_nomads_live/produce_trading_signals.py` | **Producteur de signaux** bot1 (GFS/NOMADS) — équivalent historique : `run_nomads_live_test.py` |
| `python "bots/bot1/Long Term/scripts/generate_markets.py" --limit 500` | Régénère `data/active_markets.json` depuis scan live Gamma |
| Voir `bots/bot1/MODEL_SCIENCE.md` | σ / `p_win`, lien calibrateur, feuille de route GEFS |
| Voir `deploy/systemd/README.md` | Planifier des exécutions répétées de `update_bot.sh` (service ou timer systemd) |

### Chemins bot1 (NOAA → finance → settlement)

Par défaut : fichiers sous `data/` (`trading_signals.json`, `trading_signals_meta.json`, `portfolio.sqlite`, etc.). Variables optionnelles : `TRADING_SIGNALS_FILE`, `TRADING_SIGNALS_META_FILE`, `PORTFOLIO_SQLITE_PATH`, `NOAA_WATCHDOG_STATE_PATH`, `LAST_NOAA_RUN_FILE` (chemins absolus ou relatifs à la racine du repo). Détail : `core/bot1_data_paths.py`, `.env.example`.

`python main_compartment.py --bot 1` préremplit ces variables vers `data/bots/bot_1/…`. `bash update_bot.sh` exporte des chemins **absolus** pour tout le cycle (y compris le test du manifeste NOAA).

## Diagnostics / Polymarket

| Commande | Rôle (bref) |
|----------|----------------|
| `.venv/bin/python status.py` | JSON pnl_tracker + derniers trades `portfolio.sqlite` (finance_engine) ; pas l’état CLOB brut |
| `.venv/bin/python bots/bot1/execution/status_live.py` | Wallet, ordres CLOB, balances — plus proche du site Polymarket |
| `python bots/bot1/execution/test_wallet_to_polymarket_flow.py --verbose --proxy-audit` | Test wallet + proxy + CLOB |
| `… --market-id 0x…` | Résout marché précis |
| `… --token-id …` | Cible token CLOB direct |
| `… --execute-live` | Envoie ordre réel (risque) |
| `python bots/bot1/execution/status_live.py` | Vérifie CLOB + balances |

### Test E2E « complet » (wallet → chaîne → Polymarket)

Remplace `0x…` par un **vrai** `condition_id` ou utilise `--token-id`.

**Simulation** (signe mais **ne poste pas** d’ordre réel) :

```bash
python bots/bot1/execution/test_wallet_to_polymarket_flow.py --verbose --proxy-audit --market-id 0xTON_CONDITION_ID_REEL
```

**Live** (ordre **réel** sur le CLOB — argent engagé) :

```bash
python bots/bot1/execution/test_wallet_to_polymarket_flow.py --verbose --proxy-audit --execute-live --market-id 0xTON_CONDITION_ID_REEL
```

## Utilitaires marchés

| Commande | Rôle (bref) |
|----------|----------------|
| `python -c "import sys; sys.path.insert(0,'.'); from scanners.market_scanner import scan_weather_markets; m=scan_weather_markets(limit=80); print(m[0].condition_id if m else 'aucun')"` | Affiche un `condition_id` |

## Fichiers logs (exemples)

| Chemin | Rôle (bref) |
|--------|----------------|
| `data/logs/e2e_payment_flow_*.log` | Journaux test E2E paiement |
| `logs/finance.log` | Sortie `finance_engine` via `update_bot.sh` |
| `logs/settlement_watcher.log` | `settlement_watcher` (même cycle que `update_bot.sh`) |
| `logs/exit_engine.log` | `exit_engine` / settler |
| `logs/update_bot_cycle.log` | Une ligne JSON par cycle : codes de sortie NOAA / finance / watcher / exit (`UPDATE_BOT_CYCLE_SUMMARY_LOG` pour surcharger le chemin) |

Variable optionnelle : `MAIN_DEPLOY_BANNER=1` — dans `main.py`, log INFO unique (remplace l’ancien `print` de déploiement).

---

*Les variables sensibles restent dans `.env` (non versionné). Sur le serveur, copier/coller le `.env` localement ne se fait pas via `git push`.*
