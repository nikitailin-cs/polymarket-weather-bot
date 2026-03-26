# Commandes Telegram (`monitoring/telegram_command_bot.py`)

Bot en **long polling** : rapports live, positions, derniers cycles.  
Les commandes ne sont traitées que si le message provient du chat autorisé (`TELEGRAM_CHAT_ID`).

## Prérequis

- Variables d’environnement (souvent dans `.env`) : `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- Lancement : `python monitoring/telegram_command_bot.py` (ou service systemd / tmux).

## Liste des commandes

| Commande | Alias | Rôle |
|----------|--------|------|
| `/help` | `/start` | Aide : liste des commandes. |
| `/ping` | — | Test de présence (`pong`). |
| `/health` | `/status` | État systemd (`polymarket-bot1-adaptive`, `polymarket-bot-rt.timer`), santé Bot2 RT, résumé manifeste NOAA Bot1. |
| `/lastcycle` | — | Dernier **résumé** du cycle `logs/update_bot_cycle.log` (JSON parsé : étapes, code agrégé, Δ marchés Bot1 si présent). |
| `/daemon` | — | Fin de journal `logs/bot1_adaptive_daemon.log` (lignes courtes). |
| `/bot1meta` | — | Résumé lisible de `trading_signals_meta.json` (pas de JSON brut). |
| `/bot2meta` | — | Résumé lisible de `realtime_signals_meta.json` (manifeste Bot2 RT). |
| `/positions` | — | Derniers trades **Bot1** (`executed_trades`) : cartes avec question, dates contrat, oracle/station, **~USDC** engagé, statut. |
| `/open` | `/ouvert`, `/positions_open` | Positions **OPEN** uniquement : **Bot1** puis **Bot2**, détail marché via `data/active_markets.json` quand disponible. |
| `/positions2` | `/pos2` | Positions **Bot2** récentes ; les **OPEN** sont listées en premier. |
| `/rt` | `/rthealth` | Santé signaux temps réel (meta, âge, statut, marchés traités). |
| `/markets` | — | Scan Gamma météo (peut prendre ~10–30 s) ; aperçu des marchés trouvés. |

## Notes

- Les réponses sont en **HTML** (gras, code) pour Telegram ; pas de gros blocs JSON.
- Pour `/positions` et `/open`, si un `market_id` n’est plus dans `data/active_markets.json`, l’affichage indique **hors registre** et s’appuie sur les champs SQLite.
- Commande inconnue : message invitant à utiliser `/help`.

## Voir aussi

- `monitoring/telegram_command_bot.py` — implémentation et comportement exact.
