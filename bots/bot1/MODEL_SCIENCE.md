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

## Fichiers liés

- `bots/bot1/noaa_nomads_live/run_nomads_live_test.py` — `calculate_probability`, `_resolve_sigma_for_target`
- `signals/sources/model_calibrator.py` — biais ville/station, `get_sigma_hint_for_key`
- `signals/probability_engine.py` — logique σ plus riche pour le bot **scanner** (`main.py`), autre univers de marchés
