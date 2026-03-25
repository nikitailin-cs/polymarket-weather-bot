# Bot1 — modèle & science (feuille de route)

Pipeline ops stable : ce fichier décrit les **pistes** stats/météo au-delà du producteur de signaux NOAA actuel.

## Sigma (`p_win` gaussien)

- **Défaut historique** : σ = 1,5 pour température **et** précip (même formule qu’avant) ; les pluies en mm gagnent souvent à utiliser un σ plus grand (ex. `BOT1_PROB_SIGMA_PRECIP=5`).
- **`BOT1_PROB_SIGMA`** : force un σ unique pour tous les marchés (override total).
- **`BOT1_PROB_SIGMA_TEMPS` / `BOT1_PROB_SIGMA_PRECIP`** : défauts si pas d’override global.
- **`BOT1_USE_BIAS_STD_FOR_SIGMA=1`** : si une entrée existe dans `model_bias.json` pour la **même clé** que `station_code` du marché (ex. ICAO en minuscules), avec assez d’observations, on utilise un σ dérivé de `bias_std` (voir `get_sigma_hint_for_key` dans `signals/sources/model_calibrator.py`).

**Lien `model_calibrator`** : le calibrateur historique est centré **ville** + prédictions enregistrées par `main.py` / `record_prediction`. Le pipeline bot1 NOAA ne remplit pas automatiquement ces entrées par station. Pour exploiter `BOT1_USE_BIAS_STD_FOR_SIGMA`, il faut soit aligner les clés du JSON sur les codes station des marchés, soit étendre le calibrateur pour enregistrer des erreurs **par station ICAO** à partir des runs GFS (travail ultérieur).

## GEFS (gros chantier)

Objectif : remplacer le **GFS déterministe** unique par une **distribution** (31 membres) → intervalles de confiance, `p_win` comme probabilité empirique ou mélange gaussien par membre.

Pistes techniques (réutiliser `noaa_engine` : lots membres, limites RAM) :

1. URLs NOMADS/S3 par membre + même chaîne idx / byte-range / `wgrib2` qu’aujourd’hui.
2. Agrégation par station : échantillon de Tmax ou pluie cumulée par membre.
3. `p_win` = fraction des membres qui satisfont le contrat, ou CDF Monte Carlo.

Prérequis : budget réseau/CPU, politique de parallélisme stricte (déjà esquissée dans `noaa_engine` pour GEFS).

## Backlog — alignement GFS ↔ jour de contrat (curation)

**Risque** : le bot1 est **probabiliste** et lit des fenêtres GFS (`f00`…`f24`, etc.). La pendule locale du lieu n’est pas le même piège que pour le RT METAR ; l’ennemi silencieux, c’est la **curation** : si le marché Polymarket vise le **26** mais que l’agrégat GRIB utilisé correspond en pratique au **28** (mauvaise run, mauvais horizons, mauvaise lecture du *valid time*), le bot peut produire un `p_win` très confiant pour **la mauvaise journée**.

**Principe** (même discipline d’esprit que le bot2 RT, mais **à l’ingestion GRIB**, pas au moment du trade) :

1. Enrichir le registre (`data/active_markets.json` / `MarketTarget`) avec une **`target_date`** (YYYY-MM-DD) par marché = jour calendaire de résolution du contrat côté lieu (IANA du site / station).
2. Dans `run_nomads_live_test.py` (boucle `wgrib2` / agrégation), pour chaque tranche ou agrégat utilisé, calculer le **valid time** GFS (run + `forecast_hour`) et le convertir en **date locale** cohérente avec le contrat.
3. **Gate** : si la date locale du *valid time* traité ne correspond pas à `target_date` (selon règles métier : jour unique vs plage multi-jours plus tard), **rejeter** la donnée — **avant** `calculate_probability` / écriture signal.
4. Documenter le cas **multi-jours** (plage) comme pour le RT : `target_date_start` / `target_date_end` si un jour on étend au-delà du journalier.

Tant que ce gate n’existe pas, la qualité repose entièrement sur l’opérateur et sur la cohérence implicite registre ↔ fenêtre GFS.

### Plan de refactor — `_forecast_hours_for_target` (grille dynamique)

Aujourd’hui, pour `temp_max` / `temp_min` / `temp_avg`, la fonction renvoie une grille **fixe** `(0, 3, 6, …, 24)` : cela **ancre** la prise de données au **premier jour** de la prévision depuis la run GFS choisie, **sans** lien avec la date du contrat Polymarket.

**Objectif** : ne télécharger et n’agrégier que les échéances dont le *valid time* (UTC) tombe dans la **fenêtre du jour cible** (`target_date`) en heure **locale** du site (station / fuseau du contrat).

1. **Calcul du delta (offset en heures)**  
   - Entrées : `target_date` (date calendaire du contrat), `timezone` (IANA) du lieu, et la **run GFS retenue** (`chosen_run` : jour + `cycle_utc`) telle que `noaa_engine` la définit.  
   - Construire l’instant de référence de la run (ex. `2026-03-24 12:00 UTC` pour une cycle 12Z).  
   - Pour le jour `target_date` en local : définir une fenêtre `[min_utc, max_utc)` (ex. minuit→minuit local, ou règle métier alignée oracle).  
   - Pour chaque échéance GFS `fh ∈ {0,3,6,…}` : `valid_utc = run_ref_utc + fh` (modèle exact selon convention GFS / NOMADS déjà utilisée dans le projet).  
   - **Dériver** l’ensemble des `fh` tels que `valid_utc` intersecte la fenêtre du jour cible — souvent une **bande de 24 h** en pas de 3 h, ex. `(60, 63, 66, …, 84)` si le jour cible commence ~60 h après la référence de run.  
   - Si aucun `fh` dans les limites du produit (ex. GFS max 384 h mais run trop ancienne), **skip** ou **changer de run** (voir ci‑dessous).

2. **Grille dynamique**  
   - Remplacer le retour figé `(0, …, 24)` par `tuple(sorted(fh_set))` calculé à partir de `target_date` + run + TZ.  
   - Exemple illustratif (valeurs fictives) :  
     `return (60, 63, 66, 69, 72, 75, 78, 81, 84)`  
     pour couvrir **une** journée cible avec des sorties 3 h.

3. **Téléchargement ciblé**  
   - La boucle existante (`build_gfs_*_url`, `fetch_idx_text`, `get_range_bytes`, `wgrib2`) s’exécute **déjà** par `fh` ; il suffit qu’elle n’itère que sur les `fh` de la grille dynamique.  
   - **Ne pas** télécharger les `f00`…`f24` si le contrat cible un autre jour.

4. **Points d’attention implémentation**  
   - **Probe actuel** : la première étape du script teste la disponibilité d’un `.idx` avec un seul `fh` (CLI `--forecast-hours`, défaut 3). Il faudra soit sonder un `fh` dans la grille cible, soit le **max** `fh` requis parmi tous les marchés du run.  
   - **Plusieurs marchés** : chaque `MarketTarget` peut avoir `target_date` différent → grilles ou **réunion** des `fh` (union) pour éviter N× téléchargements du même fichier GRIB.  
   - **Précip / autres variables** : aujourd’hui `return (fallback_fh,)` ; même logique de **delta** si l’agrégat doit correspondre au jour de résolution (ex. cumul sur 24 h du jour cible).  
   - **Validation** : après extraction, vérifier que le *valid time* de chaque tranche utilisée (ou son agrégat) correspond bien à `target_date` en local (gate défensif).

Ce plan remplace l’« hallucination » par une correspondance explicite **run GFS + `fh` ↔ jour calendaire du contrat**.

## Roadmap Bot1 — extracteur Polymarket → `target_date` (Étape 1)

**But** : avant tout téléchargement GRIB, figer la **date calendaire du contrat** dans le registre (`data/active_markets.json` → futur champ `target_date`, voire `timezone` explicite pour le lieu). On passe d’un bot qui « regarde demain » par défaut à un bot **sniper** calé sur **le jour exact** du marché.

### Approche hybride recommandée (LLM + regex)

| Couche | Rôle |
|--------|------|
| **LLM** (prompt strict, ex. Gemini / Claude) | Initialisation / désambiguïsation sur titres longs ou formulations rares : *« Extrais la date cible au format YYYY-MM-DD ; année courante du contexte (ex. 2026). Réponse : JSON uniquement. »* |
| **Regex / parse déterministe** | Validation et rejet : si la sortie ne matche pas `^\d{4}-\d{2}-\d{2}$` ou date impossible → **pas d’écriture** dans le registre. |

Le projet a déjà un **précédent** d’hybride dans `core/contract_parser.py` : `_regex_parse` puis repli `_llm_parse` dans `parse_contract`. L’extracteur `target_date` peut réutiliser la même philosophie (regex d’abord sur le titre, LLM si besoin).

### Sources de vérité (ordre suggéré)

1. **API Gamma / marché** : si `end_date` ou champ équivalent est fiable pour le **jour de résolution**, l’utiliser en priorité (moins de bruit qu’un LLM seul).
2. **Titre** : regex sur motifs du type `on March 27`, `March 27, 2026`, etc.
3. **LLM** : uniquement si 1–2 insuffisants, avec sortie JSON contrainte puis **même** validation regex.

### Persistance

- Écrire `target_date` (string `YYYY-MM-DD`) sur chaque entrée de `markets[]` dans `active_markets.json`.
- Étendre `ActiveMarket` / `MarketTarget` dans `market_manager.py` pour charger le champ (et éventuellement `timezone` IANA si ce n’est pas dérivé de la station).

Tant que cette étape n’est pas en prod, la grille GFS dynamique (section précédente) n’a pas de date fiable à consommer.

### Étape 2 — Moteur temporel (implémenté : `calculate_gfs_horizons`)

Module : `bots/bot1/noaa_nomads_live/gfs_horizons.py`.

- **`gfs_run_start_utc_from_parts(day, cycle_utc)`** — instant UTC de la run (00 / 06 / 12 / 18 Z), même convention que `noaa_engine._cycle_to_datetime`.
- **`calculate_gfs_horizons(run_start_utc, target_date, locality_tz, …)`** — pour chaque `fh` multiple de 3 jusqu’à `max_fh` (défaut 384), calcule `valid_utc = run_start_utc + fh` et conserve les `fh` dont l’heure **locale** (IANA) tombe sur `target_date` (minuit → minuit).

**Branché** dans `_forecast_hours_for_target` (`run_nomads_live_test.py`) dès que le registre contient `target_date` + `timezone` (IANA) pour le marché — sinon grille legacy f000–f024 + warning.

Tests unitaires : `tests/test_all.py` — `TestGfsHorizons`.

### Étape 3 — Ingestion `wgrib2` (refactor `_forecast_hours_for_target`)

- `active_markets.json` : champs optionnels **`target_date`** (YYYY-MM-DD) et **`timezone`** (alias **`locality_tz`**) — jour du contrat en heure locale du lieu.
- `MarketTarget` / `ActiveMarket` : champs `target_date`, `locality_tz` (chargés depuis le JSON).
- Pour **temp_max / temp_min / temp_avg** : `_forecast_hours_for_target` appelle `calculate_gfs_horizons` avec `gfs_run_start_utc_from_parts(chosen_run.day, chosen_run.cycle_utc)` ; la boucle ne télécharge que les `.idx` / tranches **pgrb2** pour ces `fh` (plus de balayage systématique f000–f024 si cible renseignée).
- **precip** : inchangé (`fallback_fh` seul).
- Si `target_date` / `timezone` absents ou grille vide : **fallback** legacy + `logging.warning` (compat déploiements existants).

### Étape 4 — σ dynamique (horizon GFS, garde-fou science)

Plus l’échéance ``fh`` est lointaine, plus le GFS est incertain : le **σ** de la Gaussienne ``p_win`` est multiplié par ``sigma_horizon_multiplier(max fh)`` (voir ``gfs_horizons.sigma_horizon_multiplier``).

- **Défaut** : au-delà de 24 h, ``mult = 1 + 0,5 × (fh − 24) / 24`` → **f120 ≈ ×3** par rapport à f24.
- **Env** : ``BOT1_SIGMA_HORIZON_REF_H``, ``BOT1_SIGMA_HORIZON_SLOPE``, ou désactiver avec ``BOT1_DISABLE_SIGMA_HORIZON=1``.
- **Implémentation** : ``_resolve_sigma_for_target(..., horizon_hours=max(fh_series))`` dans ``run_nomads_live_test.py`` ; logs : ``sigma=… (base=… mult=… fh_max=…)``.

## Fichiers liés

- `core/contract_parser.py` — `parse_contract`, `_regex_parse` / `_llm_parse` (modèle hybride existant pour les specs météo)
- `bots/bot1/noaa_nomads_live/gfs_horizons.py` — `calculate_gfs_horizons`, `gfs_run_start_utc_from_parts`, `sigma_horizon_multiplier` (σ vs horizon)
- `bots/bot1/noaa_nomads_live/run_nomads_live_test.py` — `calculate_probability`, `_resolve_sigma_for_target`, boucle `wgrib2` (point d’accroche futur pour le gate *valid time* vs `target_date`)
- `bots/bot1/Long Term/scripts/market_manager.py` — `MarketTarget`, chargement `active_markets.json` (enrichissement futur `target_date`)
- `signals/sources/model_calibrator.py` — biais ville/station, `get_sigma_hint_for_key`
- `signals/probability_engine.py` — logique σ plus riche pour le bot **scanner** (`main.py`), autre univers de marchés
