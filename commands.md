# Commandes serveur (référence rapide)

Chemins type : `~/polymarket-weather-bot`. Préfixer par `cd` + activer le venv si besoin.

| Commande | Rôle (bref) |
|----------|----------------|
| `cd ~/polymarket-weather-bot` | Va dossier projet |
| `source .venv/bin/activate` | Active interpréteur Python projet |
| `git pull` | Met à jour code distant |
| `pip install -r requirements.txt` | Installe dépendances Python |

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

| Commande | Rôle (bref) |
|----------|----------------|
| `python main.py` | Lance boucle trading continue |
| `python signals/edge_detector.py` | Affiche opportunités ponctuelles |
| `bash update_bot.sh` | Enchaîne NOAA test + moteur finance |
| Voir `deploy/systemd/README.md` | Service systemd bot1 en boucle (NOAA + finance) ou timer |

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

---

*Les variables sensibles restent dans `.env` (non versionné). Sur le serveur, copier/coller le `.env` localement ne se fait pas via `git push`.*
