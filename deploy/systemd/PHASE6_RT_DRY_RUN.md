# Phase 6.5 — Validation dry-run (bot RT)

Avant d’activer `polymarket-bot-rt.timer` en production avec de vrais ordres CLOB :

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
