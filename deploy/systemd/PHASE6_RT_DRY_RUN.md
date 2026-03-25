# Phase 6.5 — Validation dry-run (bot RT)

Avant d’activer `polymarket-bot-rt.timer` en production avec de vrais ordres CLOB :

## Signature proxy (critique avant de couper le dry-run)

Le moteur `bots/bot2_realtime/execution/finance_engine_rt.py` appelle **`apply_bot2_clob_signing_env()`** tout de suite après `load_dotenv`, **avant** la première utilisation du `ClobClient` (y compris lecture L2). Cette fonction **force** pour le processus :

- `POLYMARKET_IS_CONTRACT=true`
- `TYPE=1`
- `POLYMARKET_SIGNATURE_TYPE=1`

Sans cela, un `POLYMARKET_SIGNATURE_TYPE=2` déjà présent dans le `.env` pouvait rester actif → risque d’**invalid signature** hors dry-run avec le **funder proxy** Polymarket.

Ne pas désactiver le dry-run tant que tu n’as pas vérifié dans les logs une ligne du type :  
`bot2 RT: env signature CLOB forcé — … POLYMARKET_SIGNATURE_TYPE=1 (avant ClobClient)`  
et que le client loggé par `finance_engine` indique bien `signature_type=1` (et funder = adresse proxy attendue).

---

1. **Variables d’environnement** (dans `polymarket-bot-rt.service` ou override systemd) :
   - `DRY_RUN=1`
   - `FINANCE_RT_DRY_RUN=1` (recommandé en plus pour le moteur RT)

   Décommenter les lignes `Environment=` prévues dans `deploy/systemd/polymarket-bot-rt.service`, ou créer un **drop-in** :

   ```bash
   sudo systemctl edit polymarket-bot-rt.service
   ```

   ```ini
   [Service]
   Environment=DRY_RUN=1
   Environment=FINANCE_RT_DRY_RUN=1
   ```

2. **Durée** : laisser tourner **24 h** minimum (plusieurs fenêtres METAR horaires).

3. **Contrôles dans `logs/update_bot_rt_cycle.log`** :
   - `produce_rt_signals` sans erreur fatale ; manifeste `status=ok` quand les stations répondent.
   - `finance_engine_rt` : kill-switch si signaux périmés ; pas de `post_order` réel (dry-run).
   - Chaîne settlement : optionnel ; en dry-run, `exit_engine` ne déclenche pas de redeem on-chain si configuré ainsi.

4. **Retour prod** : retirer `DRY_RUN` / `FINANCE_RT_DRY_RUN`, `systemctl daemon-reload`, puis `systemctl restart polymarket-bot-rt.timer`.
