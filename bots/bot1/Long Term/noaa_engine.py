"""
noaa_engine — « radar » GFS NOAA (NOMADS), sans charger de GRIB en RAM.

Objectif : construire les URLs exactes des sorties GFS 0.25° et vérifier
leur disponibilité avec des requêtes légères (HEAD), pas de téléchargement
de fichiers ~500 Mo–1 Go+.

Anatomie d'une URL (prod GFS) :
  {BASE}/gfs.YYYYMMDD/HH/atmos/gfs.tHHz.pgrb2.0p25.fXXX

  - gfs.YYYYMMDD : date UTC du run (pas forcément « aujourd’hui » si on est
    juste après minuit UTC — le dernier run peut être la veille).
  - HH : cycle 00, 06, 12, 18 UTC.
  - fXXX : échéance en heures (f003 = +3 h, f024 = +24 h), sur 3 chiffres.

Référence racine publique :
  https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/

Secours (AWS Open Data — souvent plus rapide si NOMADS est saturé) :
  https://noaa-gfs-bdp-pds.s3.amazonaws.com/
  Le chemin sous le bucket est le même relatif que sous …/prod/
  (gfs.YYYYMMDD/HH/atmos/…), sans le préfixe …/com/gfs/prod/.

Politique HTTP (ping / discret / faible RAM) :
  - Vérifier la présence d’un fichier : uniquement HEAD (~quelques centaines d’octets
    de métadonnées ; 200 = présent, 404 = pas encore là). Ne jamais utiliser GET pour ça :
    urllib téléchargerait le corps (GRIB énorme) même si tu ne le « lis » pas.
  - GET : réservé au téléchargement ciblé après coup, ex. Range bytes sur le .pgrb2
    une fois les offsets connus via le fichier .idx (texte, HEAD sur le .idx d’abord).

Ping hybride NOMADS / S3 (signal de départ = .idx) :
  - ``BASE_URL_PRIORITY`` (NOMADS) puis ``BASE_URL_BACKUP`` (S3) : le script ne s’arrête pas
    si NOMADS est saturé — il commute sur S3.
  - Priorité HEAD sur le .idx NOMADS ; le GRIB peut apparaître avant l’index — on ne
    considère pas la course « lancée » sans .idx.
  - Sur S3, tester les suffixes d’index ``S3_IDX_SUFFIX_VARIANTS`` (``.idx`` et ``.grib2.idx``).
  - Sur S3, contrôle du Last-Modified (UTC) pour éviter un fichier d’hier / cache obsolète.

Anti-ban (e2-micro / IP) :
  - Jitter aléatoire sur le sleep entre deux pings (évite motif fixe type bot).
  - 3× **403** consécutifs sur NOMADS → pause **5 min** + poursuite des pings **.idx** sur S3
    uniquement jusqu’à un HEAD NOMADS à nouveau OK (proxy non géré ici — à brancher côté infra).

Chaîne de montage (résumé) — fonctions principales :

1. **Calculateur d’URL** — Run & date dans le chemin ``gfs.YYYYMMDD/HH/…`` :
   :func:`iter_candidate_runs`, :func:`build_gfs_pgrb2_url`, :func:`build_gfs_idx_url`,
   :func:`build_s3_pgrb2_url`, :func:`gfs_poll_interval_sec` / fenêtres UTC.

2. **Sentinelle (ping)** — Boucle **synchrone** : :func:`probe_idx_hybrid`, :func:`poll_until_idx_available`
   (HEAD NOMADS ; pas de corps GRIB). Pour du **async**, envelopper avec
   ``asyncio.to_thread(probe_idx_hybrid, ...)`` ou ``run_in_executor`` ; pour le téléchargement
   .idx / Range avec **aiohttp**, utiliser :func:`async_fetch_idx_text`, :func:`async_get_range_bytes`,
   :func:`async_head_get_content_length`.

3. **Basculeur (fallback)** — :func:`probe_idx_hybrid` (S3 si 503/404/403… + anti-ban +
   :func:`s3_last_modified_matches_run_day`).

4. **Extracteur d’index** — :func:`fetch_idx_text`, :func:`parse_idx_wgrib2`,
   :func:`parse_idx_wgrib2_regex` / :func:`parse_idx_line_wgrib2_regex` (regex **N:offset:…**),
   :func:`contiguous_byte_range_for_record`, :func:`byte_range_for_idx_line_containing`,
   :func:`byte_range_for_idx_line_descriptor_regex`, :func:`byte_range_for_variable_and_level`
   (recherche ex. ``:TMP:`` + ``2 m above ground`` → octets début/fin ; fin = dernier octet avant
   le début du message suivant, ou fin de fichier si dernier message).

5. **Moissonneur** — :func:`get_range_bytes` / :func:`get_range_bytes_io` (segment en RAM,
   sans fichier GRIB persistant), :func:`harvest_grib_chunk_by_idx_substring`,
   :func:`harvest_grib_chunks_by_field_substrings`, :func:`fetch_idx_and_harvest_weather_fields` ;
   :func:`head_get_content_length` pour la taille totale du pgrb2 (dernier message).

6. **Scalpel** — :func:`run_wgrib2_scalpel` (stdin → ``wgrib2 -``), :func:`run_wgrib2_scalpel_at_point`
   (``-lon`` / ``-inv``), :func:`parse_wgrib2_stdout_scalar` (sortie → ``float``).

7. **Ensemble GEFS (31 membres)** — :func:`iter_gefs_member_batches`, :func:`run_gefs_members_batched`,
   :func:`release_bytesio` : traitement par **lots** (défaut 5 membres à la fois, jamais 31 en parallèle),
   option **micro-parallèle** limitée au lot ; vidage des tampons :class:`io.BytesIO` entre membres.

Déroulement du « Ping » à l’analyse (exemple **run 12z**, dispo ~**15:30** UTC) :

1. **Sommeil** — Ex. **14:00** UTC : le .idx du 12z n’est pas attendu avant ~**15:30**.
   Pause longue : ``time.sleep`` ≈ **300 s** (± jitter) via :func:`gfs_poll_sleep_duration_sec`.
   *(Ce module est synchrone ; en asyncio, utiliser ``await asyncio.sleep(...)`` dans ta boucle.)*

2. **Alerte** — Dès **15:25** UTC (fenêtre intensive), intervalle ≈ **10 s** (± jitter) ;
   :func:`probe_idx_hybrid` envoie des **HEAD** sur l’URL du **.idx** (NOMADS puis S3 si besoin).

3. **Capture** — Ex. **15:34** : réponse **200 OK** au HEAD → le .idx est utilisable.

4. **Analyse** — :func:`fetch_idx_text` charge le .idx en RAM ; recherche d’une sous-chaîne
   ou regex sur le descripteur (ex. ``:TMP:`` + ``2 m above ground``) avec
   :func:`byte_range_for_idx_line_containing` / :func:`byte_range_for_variable_and_level` /
   :func:`byte_range_for_idx_line_descriptor_regex` pour obtenir les octets **début / fin** du
   message GRIB (l’**offset** utile est le **2ᵉ** nombre de la ligne : ``N:offset:…``).

5. **Moisson** — :func:`get_range_bytes` / :func:`get_range_bytes_io` (ou :func:`async_get_range_bytes`
   / :func:`async_get_range_bytes_io` avec **aiohttp**) : ``GET`` + en-tête
   ``Range: bytes={start}-{end}`` — uniquement le petit segment (évite de charger le pgrb2 entier).

6. **Scalpel** — le chunk est envoyé sur **stdin** (``wgrib2 -``). Pour un **point GPS** :
   :func:`run_wgrib2_scalpel_at_point` avec ``-lon LON LAT`` et ``-inv`` vers le vide (pas d’inventaire
   inutile) ; :func:`parse_wgrib2_stdout_scalar` extrait la valeur numérique (température, pluie, etc.).

**Byte-Range + .idx (RAM)** : :func:`fetch_idx_and_harvest_weather_fields` enchaîne
GET du .idx → repères d’octets → GET Range par **message GRIB** (champ météo), résultat en
``bytes`` ou :class:`io.BytesIO` en mémoire (pas de fichier .grib2 persistant pour le segment).
Chaque message = une **grille horizontale** pour ce champ (pas une
découpe « ville » : pour une vraie zone géographique, utiliser **wgrib2** après le chunk,
ex. ``-small_grib``, ``-ij``, etc.).
"""

from __future__ import annotations

import argparse
import email.utils
import io
import os
import random
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Literal, Optional, Sequence, Tuple, TypeVar

T = TypeVar("T")

# ── Constantes ───────────────────────────────────────────────────────────────

# Miroirs HTTP : priorité NOMADS ; en surcharge (502/503/504, etc.) on commute sur le backup S3.
BASE_URL_PRIORITY = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
BASE_URL_BACKUP = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/"

GFS_PROD_BASE = BASE_URL_PRIORITY
GFS_S3_BASE = BASE_URL_BACKUP

# Sur S3, le fichier d’index n’a pas toujours le même suffixe après le nom du pgrb2 :
# tester au HEAD dans cet ordre (ex. ``...f003.idx`` puis ``...f003.grib2.idx``).
S3_IDX_SUFFIX_VARIANTS: Tuple[str, ...] = (".idx", ".grib2.idx")

GFS_CYCLES_HOUR: Tuple[str, ...] = ("00", "06", "12", "18")

# Délai typique avant qu’un cycle 0.25° soit exploitable en HTTP (ordre de grandeur).
# Ajustable si tu constates trop de 404 puis succès plus tard.
DEFAULT_PUBLICATION_LAG_HOURS = 3.5

DEFAULT_HEAD_TIMEOUT_SEC = 10.0
DEFAULT_USER_AGENT = "POLYMARKET-bot1-noaa_engine/1.0"

# Ordre de grandeur réponse HEAD (headers seuls) — adapté au polling intensif sur e2-micro.
ESTIMATED_HEAD_TRANSFER_BYTES = 200

# Limite de sécurité pour un GET du fichier .idx (texte, bien plus petit que le GRIB).
MAX_IDX_FETCH_BYTES = 10 * 1024 * 1024

# Motifs typiques dans les lignes .idx GFS (wgrib2) pour cibler température / précipitation.
# Vérifier sur un .idx réel : les libellés peuvent varier légèrement (niveau, accumulation).
DEFAULT_FIELD_SUBSTRINGS_TEMP_PRECIP: Tuple[str, ...] = (
    ":TMP:2 m above ground:",
    ":APCP:surface:",
)

# GEFS : typiquement **31** membres (1 contrôle + 30 perturbés) — indices ``0 … 30``.
# Les URLs / chemins NOMADS-S3 par membre restent à construire côté appelant ; ce module fournit
# seulement le **cadencement** par lots pour limiter la RAM.
GEFS_MEMBER_COUNT: int = 31
GEFS_DEFAULT_MEMBER_BATCH_SIZE: int = 5

# ── Polling e2-micro : le GFS sort 4×/jour — pas de ping inutile hors fenêtre ─
# Début dispo ~3h30 après le run ; fenêtre « alerte » = [T_dispo − 5 min, T_dispo + 45 min]
# (ex. 12z → 15:25–16:15 UTC). Hors fenêtre : ping léger toutes les 5 min ; dans la fenêtre : 10 s.
GFS_POLL_SLEEP_SEC = 300.0
GFS_POLL_ALERT_SEC = 10.0
GFS_INTENSIVE_WINDOW_BEFORE = timedelta(minutes=5)
GFS_INTENSIVE_WINDOW_AFTER = timedelta(minutes=45)

# Jitter (anti-ban) : ajouté au délai de base (alerte ~10 s, sommeil ~300 s)
GFS_POLL_JITTER_ALERT_SEC = (1.0, 5.0)
GFS_POLL_JITTER_SLEEP_SEC = (1.0, 5.0)

# 403 répétés sur NOMADS → cooldown puis priorité S3
ANTIBAN_MAX_CONSECUTIVE_403 = 3
ANTIBAN_COOLDOWN_SEC = 300.0


@dataclass(frozen=True)
class GfsRun:
    """Identifiant d’un run GFS (date UTC du dossier + cycle)."""

    day: date
    cycle_utc: str  # "00", "06", "12", "18"


@dataclass(frozen=True)
class HeadDetail:
    """Résultat d’un HEAD (métadonnées seules)."""

    status_code: int
    ok: bool
    last_modified: Optional[datetime]
    is_service_unavailable: bool  # 503 — surcharge NOMADS typique

    @property
    def is_forbidden(self) -> bool:
        return self.status_code == 403


@dataclass(frozen=True)
class IdxPingResult:
    """Signal de départ : présence du fichier .idx (jamais le seul pgrb2 sans index)."""

    url: str
    source: Literal["nomads_idx", "s3_idx"]
    status_code: int
    last_modified: Optional[datetime]
    nomads_status_code: int


@dataclass
class AntiBanGovernor:
    """
    Limite les risques de blocage IP sur NOMADS : compte les 403 consécutifs sur le HEAD
    NOMADS ; au bout de ``max_consecutive_403``, demande une pause longue (cooldown)
    et force les pings .idx sur S3 uniquement jusqu’à ce qu’un HEAD NOMADS réussisse à nouveau.

    Proxy : non géré ici — à configurer au niveau HTTP (variable d’environnement / urllib opener).
    """

    max_consecutive_403: int = ANTIBAN_MAX_CONSECUTIVE_403
    cooldown_sec: float = ANTIBAN_COOLDOWN_SEC
    _consecutive_403: int = field(default=0, repr=False)
    _force_s3_only: bool = field(default=False, repr=False)
    _cooldown_sleep_pending: bool = field(default=False, repr=False)

    def record_nomads_response(self, detail: HeadDetail) -> None:
        """À appeler après chaque HEAD NOMADS réel (pas si NOMADS est sauté)."""
        if detail.status_code == 403:
            self._consecutive_403 += 1
            if self._consecutive_403 >= self.max_consecutive_403:
                self._cooldown_sleep_pending = True
                self._force_s3_only = True
                self._consecutive_403 = 0
        else:
            self._consecutive_403 = 0
            if detail.ok:
                self._force_s3_only = False

    def consume_cooldown_sleep(self) -> bool:
        """
        Si True, l’appelant doit exécuter ``time.sleep(cooldown_sec)`` (pause anti-ban).
        """
        if self._cooldown_sleep_pending:
            self._cooldown_sleep_pending = False
            return True
        return False

    def should_skip_nomads(self) -> bool:
        """Après trop de 403 : ne plus frapper NOMADS, uniquement S3 pour le .idx."""
        return self._force_s3_only

    def note_idx_found(self, source: Literal["nomads_idx", "s3_idx"]) -> None:
        """
        Réinitialise le compteur 403 dès qu’un .idx est trouvé.
        Lève aussi le mode « NOMADS interdit » pour retenter NOMADS au cycle suivant.
        """
        self._consecutive_403 = 0
        self._force_s3_only = False
        self._last_idx_source = source


def _normalize_cycle(cycle: str) -> str:
    c = cycle.strip().zfill(2)
    if c not in GFS_CYCLES_HOUR:
        raise ValueError(f"Cycle GFS invalide : {cycle!r} (attendu un de {GFS_CYCLES_HOUR})")
    return c


def forecast_suffix(forecast_hours: int) -> str:
    """
    Retourne le suffixe .fXXX (heures entières, 3 chiffres).
    Ex. 3 -> f003, 24 -> f024.
    """
    if forecast_hours < 0 or forecast_hours > 999:
        raise ValueError(f"forecast_hours hors plage 0..999 : {forecast_hours}")
    return f"f{forecast_hours:03d}"


def gfs_day_folder(day: date) -> str:
    """Ex. 2026-03-23 -> gfs.20260323"""
    return f"gfs.{day.strftime('%Y%m%d')}"


def _atmos_relative_path(day: date, cycle_utc: str, forecast_hours: int) -> str:
    """Chemin relatif commun : gfs.YYYYMMDD/HH/atmos/nom_fichier"""
    hh = _normalize_cycle(cycle_utc)
    folder = gfs_day_folder(day)
    fname = f"gfs.t{hh}z.pgrb2.0p25.{forecast_suffix(forecast_hours)}"
    return f"{folder}/{hh}/atmos/{fname}"


def build_gfs_pgrb2_url(
    day: date,
    cycle_utc: str,
    forecast_hours: int,
    *,
    base: str = GFS_PROD_BASE,
) -> str:
    """
    URL complète du fichier pgrb2 0.25° pour un run et une échéance donnés.
    Ne garantit pas que le fichier existe (NOMADS peut être en retard).
    """
    rel = _atmos_relative_path(day, cycle_utc, forecast_hours)
    return f"{base.rstrip('/')}/{rel}"


def build_gfs_idx_url(
    day: date,
    cycle_utc: str,
    forecast_hours: int,
    *,
    base: str = GFS_PROD_BASE,
) -> str:
    """URL du fichier index GRIB (.idx) associé au pgrb2 (même chemin + .idx)."""
    return build_gfs_pgrb2_url(day, cycle_utc, forecast_hours, base=base) + ".idx"


def build_s3_pgrb2_url(
    day: date,
    cycle_utc: str,
    forecast_hours: int,
    *,
    base: str = GFS_S3_BASE,
) -> str:
    """URL complète sur le bucket S3 NOAA GFS (sans préfixe NOMADS /prod/)."""
    rel = _atmos_relative_path(day, cycle_utc, forecast_hours)
    return f"{base.rstrip('/')}/{rel}"


def build_s3_idx_url(
    day: date,
    cycle_utc: str,
    forecast_hours: int,
    *,
    idx_suffix: str = S3_IDX_SUFFIX_VARIANTS[0],
    base: str = GFS_S3_BASE,
) -> str:
    """Index sur S3 : par défaut ``…fXXX.idx`` ; utiliser ``idx_suffix=``.grib2.idx`` si besoin."""
    return build_s3_pgrb2_url(day, cycle_utc, forecast_hours, base=base) + idx_suffix


def iter_s3_idx_url_candidates(
    day: date,
    cycle_utc: str,
    forecast_hours: int,
    *,
    base: str = BASE_URL_BACKUP,
) -> Iterator[str]:
    """
    URLs candidates pour l’index sur le **backup** S3 : teste chaque suffixe
    (:data:`S3_IDX_SUFFIX_VARIANTS`) jusqu’à ce qu’un HEAD réussisse.
    """
    stem = build_s3_pgrb2_url(day, cycle_utc, forecast_hours, base=base)
    for suf in S3_IDX_SUFFIX_VARIANTS:
        yield stem + suf


def _cycle_to_datetime(day: date, cycle_utc: str) -> datetime:
    hh = int(_normalize_cycle(cycle_utc))
    return datetime(day.year, day.month, day.day, hh, 0, 0, tzinfo=timezone.utc)


def iter_candidate_runs(
    now_utc: Optional[datetime] = None,
    *,
    publication_lag_hours: float = DEFAULT_PUBLICATION_LAG_HOURS,
    max_runs: int = 16,
) -> Iterator[GfsRun]:
    """
    Génère des runs GFS du plus récent « plausible » au plus ancien.

    Un run à l’instant T est considéré comme « sorti » si
    T >= run_start + publication_lag (heuristique, pas une garantie NOAA).

    Parcourt aujourd’hui et hier en UTC, cycles 18→12→6→0, puis la veille, etc.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    threshold = now_utc - timedelta(hours=publication_lag_hours)
    day0 = now_utc.date()

    runs: list[GfsRun] = []
    for delta in range(3):  # 3 jours max — largement suffisant pour trouver un fichier
        d = day0 - timedelta(days=delta)
        for hh in ("18", "12", "06", "00"):
            t_run = _cycle_to_datetime(d, hh)
            if t_run <= threshold:
                runs.append(GfsRun(day=d, cycle_utc=hh))

    # Plus récent d’abord
    runs.sort(key=lambda r: _cycle_to_datetime(r.day, r.cycle_utc), reverse=True)

    for r in runs[:max_runs]:
        yield r


def build_url_for_run(run: GfsRun, forecast_hours: int) -> str:
    """URL pgrb2 0.25° (NOMADS) pour un GfsRun et une échéance."""
    return build_gfs_pgrb2_url(run.day, run.cycle_utc, forecast_hours)


def urls_for_run_all_sources(run: GfsRun, forecast_hours: int) -> dict:
    """
    Toutes les URLs utiles pour un run/échéance : pgrb2 + .idx sur NOMADS et S3.
    Ordre conseillé pour les bornes d’octets : tester d’abord le .idx sur NOMADS,
    puis le .idx sur S3 ; le téléchargement Range du pgrb2 peut suivre sur l’URL
    la plus réactive.
    """
    d, c = run.day, run.cycle_utc
    return {
        "base_url_priority": BASE_URL_PRIORITY,
        "base_url_backup": BASE_URL_BACKUP,
        "nomads_pgrb2": build_gfs_pgrb2_url(d, c, forecast_hours, base=GFS_PROD_BASE),
        "nomads_idx": build_gfs_idx_url(d, c, forecast_hours, base=GFS_PROD_BASE),
        "s3_pgrb2": build_s3_pgrb2_url(d, c, forecast_hours),
        "s3_idx": build_s3_idx_url(d, c, forecast_hours),
        "s3_idx_candidates": list(iter_s3_idx_url_candidates(d, c, forecast_hours)),
    }


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def run_publication_time_utc(
    run: GfsRun,
    *,
    lag_hours: float = DEFAULT_PUBLICATION_LAG_HOURS,
) -> datetime:
    """
    Heure UTC à laquelle les sorties 0.25° sont en général « visibles » (ordre de grandeur).
    Ex. run 12:00 UTC → ~15:30 UTC avec lag 3h30 — moment où intensifier les HEAD sur le .idx.
    """
    return _cycle_to_datetime(run.day, run.cycle_utc) + timedelta(hours=lag_hours)


def intensive_ping_window_bounds(
    run: GfsRun,
    *,
    lag_hours: float = DEFAULT_PUBLICATION_LAG_HOURS,
) -> Tuple[datetime, datetime]:
    """
    Fenêtre de ping intensif autour de la disponibilité estimée du run.
    Table de référence (lag 3h30) :
      00z → 03:25–04:15 | 06z → 09:25–10:15 | 12z → 15:25–16:15 | 18z → 21:25–22:15 UTC
    """
    t_dispo = run_publication_time_utc(run, lag_hours=lag_hours)
    return (
        t_dispo - GFS_INTENSIVE_WINDOW_BEFORE,
        t_dispo + GFS_INTENSIVE_WINDOW_AFTER,
    )


def is_intensive_ping_window_active(
    now_utc: Optional[datetime] = None,
    *,
    lag_hours: float = DEFAULT_PUBLICATION_LAG_HOURS,
) -> bool:
    """True si maintenant est dans une fenêtre 10 s (ping intensif)."""
    now_utc = _ensure_utc(now_utc or datetime.now(timezone.utc))
    d0 = now_utc.date()
    for d in (d0 - timedelta(days=1), d0, d0 + timedelta(days=1)):
        for hh in GFS_CYCLES_HOUR:
            run = GfsRun(day=d, cycle_utc=hh)
            w0, w1 = intensive_ping_window_bounds(run, lag_hours=lag_hours)
            if w0 <= now_utc <= w1:
                return True
    return False


def active_intensive_run(
    now_utc: Optional[datetime] = None,
    *,
    lag_hours: float = DEFAULT_PUBLICATION_LAG_HOURS,
) -> Optional[GfsRun]:
    """Si on est en fenêtre intensive, retourne le run GFS concerné."""
    now_utc = _ensure_utc(now_utc or datetime.now(timezone.utc))
    d0 = now_utc.date()
    for d in (d0 - timedelta(days=1), d0, d0 + timedelta(days=1)):
        for hh in GFS_CYCLES_HOUR:
            run = GfsRun(day=d, cycle_utc=hh)
            w0, w1 = intensive_ping_window_bounds(run, lag_hours=lag_hours)
            if w0 <= now_utc <= w1:
                return run
    return None


def gfs_poll_interval_sec(now_utc: Optional[datetime] = None) -> float:
    """
    Intervalle entre deux pings : 10 s en fenêtre intensive, sinon 5 min.
    À utiliser sur instance faible (e2-micro) pour limiter CPU / requêtes inutiles.
    """
    return (
        GFS_POLL_ALERT_SEC
        if is_intensive_ping_window_active(now_utc)
        else GFS_POLL_SLEEP_SEC
    )


def gfs_poll_mode(now_utc: Optional[datetime] = None) -> Literal["sleep", "alert"]:
    """Mode sommeil (5 min) ou alerte (10 s)."""
    return "alert" if is_intensive_ping_window_active(now_utc) else "sleep"


def gfs_poll_sleep_duration_sec(
    now_utc: Optional[datetime] = None,
    *,
    use_jitter: bool = True,
) -> float:
    """
    Délai avant le prochain ping : base (10 s ou 300 s) + **jitter** aléatoire anti-ban.
    Ex. alerte : ~ ``10 + uniform(1, 5) `` s ; sommeil : ~ ``300 + uniform(1, 5)`` s.
    """
    base = gfs_poll_interval_sec(now_utc)
    if not use_jitter:
        return base
    if base <= GFS_POLL_ALERT_SEC + 0.01:
        lo, hi = GFS_POLL_JITTER_ALERT_SEC
    else:
        lo, hi = GFS_POLL_JITTER_SLEEP_SEC
    return base + random.uniform(lo, hi)


def next_publication_after(
    now_utc: Optional[datetime] = None,
    *,
    lag_hours: float = DEFAULT_PUBLICATION_LAG_HOURS,
) -> Optional[Tuple[GfsRun, datetime]]:
    """
    Prochain instant UTC où une *nouvelle* sortie est attendue (strictement après maintenant).
    Ex. à 14:00 UTC → (12z du jour, 15:30) ; à 16:00 UTC → (18z du jour, 21:30).
    """
    now_utc = _ensure_utc(now_utc or datetime.now(timezone.utc))
    d0 = now_utc.date()
    best: Optional[Tuple[GfsRun, datetime]] = None
    for d in (d0 - timedelta(days=1), d0, d0 + timedelta(days=1), d0 + timedelta(days=2)):
        for hh in GFS_CYCLES_HOUR:
            run = GfsRun(day=d, cycle_utc=hh)
            t_av = run_publication_time_utc(run, lag_hours=lag_hours)
            if t_av > now_utc and (best is None or t_av < best[1]):
                best = (run, t_av)
    return best


def gfs_poll_status(
    now_utc: Optional[datetime] = None,
    *,
    lag_hours: float = DEFAULT_PUBLICATION_LAG_HOURS,
) -> dict:
    """
    Résumé pour logs : mode, intervalle, run actif (fenêtre), prochaine publication.
    """
    now_utc = _ensure_utc(now_utc or datetime.now(timezone.utc))
    mode = gfs_poll_mode(now_utc)
    interval = gfs_poll_interval_sec(now_utc)
    active = active_intensive_run(now_utc, lag_hours=lag_hours)
    nxt = next_publication_after(now_utc, lag_hours=lag_hours)
    return {
        "now_utc": now_utc.isoformat(),
        "mode": mode,
        "interval_sec": interval,
        "active_run": active,
        "next_publication": {"run": nxt[0], "at_utc": nxt[1].isoformat()} if nxt else None,
    }


def recommended_head_interval_sec(
    now_utc: datetime,
    run: GfsRun,
    *,
    lag_hours: float = DEFAULT_PUBLICATION_LAG_HOURS,
    fast_before_min: float = 2.0,
    fast_after_min: float = 45.0,
    fast_sec: float = 1.0,
    slow_sec: float = 60.0,
) -> float:
    """
    Intervalle conseillé entre deux HEAD : ~1 s autour de (run + lag), sinon plus lent.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    t_pub = run_publication_time_utc(run, lag_hours=lag_hours)
    w0 = t_pub - timedelta(minutes=fast_before_min)
    w1 = t_pub + timedelta(minutes=fast_after_min)
    if w0 <= now_utc <= w1:
        return fast_sec
    return slow_sec


def _parse_last_modified(raw: Optional[str]) -> Optional[datetime]:
    """Parse l’en-tête HTTP Last-Modified (RFC) en datetime UTC."""
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def s3_last_modified_matches_run_day(
    run: GfsRun,
    last_modified: Optional[datetime],
) -> bool:
    """
    Sur S3, rejette un objet manifestement trop ancien (ex. cache / mauvais jour).
    Règle : la date UTC du Last-Modified doit être >= au jour du run dans l’URL (gfs.YYYYMMDD).
    """
    if last_modified is None:
        return False
    lm_day = last_modified.astimezone(timezone.utc).date()
    return lm_day >= run.day


def head_detail(
    url: str,
    *,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
) -> HeadDetail:
    """
    HEAD complet : code HTTP, succès 2xx, Last-Modified si présent, détection 503.
    Aucun corps de fichier téléchargé.
    """
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            code = resp.getcode()
            lm = resp.headers.get("Last-Modified")
            return HeadDetail(
                status_code=code,
                ok=200 <= code < 300,
                last_modified=_parse_last_modified(lm),
                is_service_unavailable=(code == 503),
            )
    except urllib.error.HTTPError as e:
        lm = e.headers.get("Last-Modified") if e.headers else None
        code = e.code
        return HeadDetail(
            status_code=code,
            ok=200 <= code < 300,
            last_modified=_parse_last_modified(lm),
            is_service_unavailable=(code == 503),
        )
    except Exception:
        return HeadDetail(
            status_code=0,
            ok=False,
            last_modified=None,
            is_service_unavailable=False,
        )


def head_ok(
    url: str,
    *,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
) -> bool:
    """
    Sonde la présence d’une ressource : **HEAD uniquement** (pas de corps de fichier).

    - 2xx → fichier présent (ou équivalent) ; utile pour .idx / .pgrb2 en mode « ping ».
    - 404 → pas encore disponible (ou chemin invalide).
    - Transfert typique ~`ESTIMATED_HEAD_TRANSFER_BYTES` octets (métadonnées), pas le GRIB.

    Ne pas remplacer par GET pour tester l’existence : le corps serait téléchargé.

    Sur certains serveurs, HEAD peut être refusé (405, etc.) ; dans ce cas, retourne False.
    """
    return head_detail(url, timeout_sec=timeout_sec, user_agent=user_agent).ok


def probe_idx_hybrid(
    run: GfsRun,
    forecast_hours: int,
    *,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
    require_s3_last_modified: bool = True,
    anti_ban: Optional[AntiBanGovernor] = None,
) -> Optional[IdxPingResult]:
    """
    Double vérification « signal de départ » = **présence du .idx** (pas du seul pgrb2).

    1. **NOMADS (priorité)** : HEAD sur le .idx. Si 2xx → succès immédiat.

    2. **S3 (``BASE_URL_BACKUP``)** : si NOMADS (``BASE_URL_PRIORITY``) est en surcharge ou refus,
       le script **continue** : HEAD sur l’index S3. Deux suffixes possibles pour l’index
       (``S3_IDX_SUFFIX_VARIANTS`` : ``.idx`` puis ``.grib2.idx``) sont testés jusqu’à succès.
       Si le HEAD S3 est 2xx, on exige un **Last-Modified** cohérent avec le jour du run
       (`require_s3_last_modified`) sauf désactivé.

    3. Si NOMADS est **404** ou **403** (forbidden / pas prêt), on tente aussi S3.

    4. **Anti-ban** : si ``anti_ban`` est fourni, les réponses NOMADS sont enregistrées ; après
       plusieurs **403** d’affilée, NOMADS est évité (S3 seul) jusqu’à HEAD NOMADS OK.

    Retourne ``IdxPingResult`` ou ``None``.
    """
    u_n = build_gfs_idx_url(run.day, run.cycle_utc, forecast_hours, base=BASE_URL_PRIORITY)

    skip_nomads = bool(anti_ban and anti_ban.should_skip_nomads())

    if skip_nomads:
        nomads = HeadDetail(0, False, None, False)
    else:
        nomads = head_detail(u_n, timeout_sec=timeout_sec, user_agent=user_agent)
        if anti_ban:
            anti_ban.record_nomads_response(nomads)

    if nomads.ok:
        if anti_ban:
            anti_ban.note_idx_found("nomads_idx")
        return IdxPingResult(
            url=u_n,
            source="nomads_idx",
            status_code=nomads.status_code,
            last_modified=nomads.last_modified,
            nomads_status_code=nomads.status_code,
        )

    if skip_nomads:
        try_s3 = True
    else:
        nomads_overloaded = nomads.status_code in (502, 503, 504)
        try_s3 = nomads_overloaded or nomads.status_code in (404, 403)

    if not try_s3:
        return None

    # Backup S3 : ne pas s’arrêter si un format de nom d’index échoue — tester .idx puis .grib2.idx.
    for u_s in iter_s3_idx_url_candidates(run.day, run.cycle_utc, forecast_hours, base=BASE_URL_BACKUP):
        s3 = head_detail(u_s, timeout_sec=timeout_sec, user_agent=user_agent)
        if not s3.ok:
            continue
        if require_s3_last_modified and not s3_last_modified_matches_run_day(run, s3.last_modified):
            continue
        if anti_ban:
            anti_ban.note_idx_found("s3_idx")
        return IdxPingResult(
            url=u_s,
            source="s3_idx",
            status_code=s3.status_code,
            last_modified=s3.last_modified,
            nomads_status_code=nomads.status_code,
        )
    return None


def probe_idx_nomads_then_s3(
    run: GfsRun,
    forecast_hours: int,
    *,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    anti_ban: Optional[AntiBanGovernor] = None,
) -> Optional[Tuple[str, str]]:
    """
    Compatibilité : même logique que :func:`probe_idx_hybrid`, retour (url, label) ou None.
    """
    r = probe_idx_hybrid(run, forecast_hours, timeout_sec=timeout_sec, anti_ban=anti_ban)
    if r is None:
        return None
    return r.url, r.source


def probe_pgrb2_nomads_then_s3(
    run: GfsRun,
    forecast_hours: int,
    *,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
) -> Optional[Tuple[str, str]]:
    """HEAD sur le pgrb2 : NOMADS puis S3. Retourne (url, 'nomads_pgrb2'|'s3_pgrb2')."""
    u_n = build_gfs_pgrb2_url(run.day, run.cycle_utc, forecast_hours, base=GFS_PROD_BASE)
    if head_ok(u_n, timeout_sec=timeout_sec):
        return u_n, "nomads_pgrb2"
    u_s = build_s3_pgrb2_url(run.day, run.cycle_utc, forecast_hours)
    if head_ok(u_s, timeout_sec=timeout_sec):
        return u_s, "s3_pgrb2"
    return None


def _http_range_get_headers(start: int, end: int, *, user_agent: str) -> dict[str, str]:
    """En-têtes pour un **GET** avec plage d’octets (RFC 7233) — segment ``[start, end]`` inclusif."""
    return {
        "User-Agent": user_agent,
        "Range": f"bytes={start}-{end}",
    }


def get_range_bytes(
    url: str,
    start: int,
    end: int,
    *,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
) -> bytes:
    """
    **GET** avec en-têtes ``{"User-Agent": …, "Range": f"bytes={start}-{end}"}`` — **pas** pour
    vérifier la présence du fichier.

    Réservé au téléchargement d’un **morceau** du GRIB après que le .idx ait fourni
    les offsets (sinon risque de charge réseau / RAM si on oublie le Range).
    Le corps est entièrement chargé en **mémoire** (objet ``bytes``), **sans** écriture d’un
    fichier .grib2 persistant sur le disque.

    Pour « est-ce que le fichier ? », utiliser uniquement :func:`head_ok`.
    Pour un tampon fichier virtuel (même données, API type fichier), préférer :func:`get_range_bytes_io`.
    """
    if start < 0 or end < start:
        raise ValueError(f"Range invalide : {start}..{end}")
    req = urllib.request.Request(
        url,
        method="GET",
        headers=_http_range_get_headers(start, end, user_agent=user_agent),
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return resp.read()


def get_range_bytes_io(
    url: str,
    start: int,
    end: int,
    *,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
) -> io.BytesIO:
    """
    Comme :func:`get_range_bytes`, mais retourne un :class:`io.BytesIO` — **fichier binaire virtuel**
    en RAM uniquement (pas de sauvegarde SSD du segment). Utile pour enchaîner avec des APIs
    qui attendent un objet « fichier » sans toucher au disque.
    """
    return io.BytesIO(get_range_bytes(url, start, end, timeout_sec=timeout_sec, user_agent=user_agent))


def head_get_content_length(
    url: str,
    *,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
) -> Optional[int]:
    """
    HEAD uniquement : lit ``Content-Length`` du pgrb2 (taille totale du fichier).
    Utile pour calculer la fin du dernier enregistrement GRIB après parsing du .idx.
    """
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            cl = resp.headers.get("Content-Length")
            return int(cl) if cl else None
    except urllib.error.HTTPError as e:
        cl = e.headers.get("Content-Length") if e.headers else None
        try:
            return int(cl) if cl else None
        except (TypeError, ValueError):
            return None
    except Exception:
        return None


def fetch_idx_text(
    url: str,
    *,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
    max_bytes: int = MAX_IDX_FETCH_BYTES,
) -> str:
    """
    Télécharge le contenu **texte** du fichier .idx (GET — fichier index, pas le GRIB multi‑Mo).
    À n’appeler qu’après :func:`probe_idx_hybrid` / HEAD OK sur le même URL.
    """
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f".idx trop volumineux (> {max_bytes} octets) — augmenter max_bytes ou vérifier l’URL")
    return data.decode("utf-8", errors="replace")


def _import_aiohttp() -> Any:
    try:
        import aiohttp
    except ImportError as e:
        raise ImportError(
            "Les fonctions async HTTP nécessitent aiohttp : pip install aiohttp"
        ) from e
    return aiohttp


async def async_fetch_idx_text(
    url: str,
    *,
    session: Optional[Any] = None,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
    max_bytes: int = MAX_IDX_FETCH_BYTES,
) -> str:
    """Version asynchrone de :func:`fetch_idx_text` (nécessite le paquet **aiohttp**)."""
    aiohttp = _import_aiohttp()
    close = False
    if session is None:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec))
        close = True
    try:
        async with session.get(url, headers={"User-Agent": user_agent}) as resp:
            resp.raise_for_status()
            data = await resp.content.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(
                f".idx trop volumineux (> {max_bytes} octets) — augmenter max_bytes ou vérifier l’URL"
            )
        return data.decode("utf-8", errors="replace")
    finally:
        if close:
            await session.close()


async def async_get_range_bytes(
    url: str,
    start: int,
    end: int,
    *,
    session: Optional[Any] = None,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
) -> bytes:
    """Version asynchrone de :func:`get_range_bytes` (nécessite **aiohttp**)."""
    if start < 0 or end < start:
        raise ValueError(f"Range invalide : {start}..{end}")
    aiohttp = _import_aiohttp()
    close = False
    if session is None:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec))
        close = True
    try:
        async with session.get(
            url,
            headers=_http_range_get_headers(start, end, user_agent=user_agent),
        ) as resp:
            resp.raise_for_status()
            return await resp.read()
    finally:
        if close:
            await session.close()


async def async_get_range_bytes_io(
    url: str,
    start: int,
    end: int,
    *,
    session: Optional[Any] = None,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
) -> io.BytesIO:
    """Version asynchrone de :func:`get_range_bytes_io` (nécessite **aiohttp**)."""
    return io.BytesIO(
        await async_get_range_bytes(
            url,
            start,
            end,
            session=session,
            timeout_sec=timeout_sec,
            user_agent=user_agent,
        )
    )


async def async_head_get_content_length(
    url: str,
    *,
    session: Optional[Any] = None,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
) -> Optional[int]:
    """Version asynchrone de :func:`head_get_content_length` (nécessite **aiohttp**)."""
    aiohttp = _import_aiohttp()
    close = False
    if session is None:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec))
        close = True
    try:
        async with session.head(url, headers={"User-Agent": user_agent}) as resp:
            if resp.status >= 400:
                return None
            cl = resp.headers.get("Content-Length")
            return int(cl) if cl else None
    finally:
        if close:
            await session.close()


def parse_idx_wgrib2(idx_text: str) -> list[Tuple[int, int, str]]:
    """
    Parse un .idx style **wgrib2** : lignes ``N:offset:…`` (N = n° d’enregistrement, offset en octets).

    Retourne une liste ``(record_number, byte_offset, reste_de_ligne)``.
    """
    out: list[Tuple[int, int, str]] = []
    for raw in idx_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        try:
            rec = int(parts[0])
            off = int(parts[1])
        except ValueError:
            continue
        rest = parts[2] if len(parts) > 2 else ""
        out.append((rec, off, rest))
    return out


# Ligne wgrib2 : ``N:offset:descripteur`` — **N** = n° d’enregistrement, **offset** = octet de début
# du message dans le pgrb2 (c’est le nombre après le **deuxième** « : », pas le premier).
WGIB2_IDX_LINE_RE = re.compile(r"^(\d+):(\d+):(.*)$")


def parse_idx_line_wgrib2_regex(raw_line: str) -> Optional[Tuple[int, int, str]]:
    """
    Parse **une** ligne .idx wgrib2 par regex : ``(record, offset_octets, descripteur)`` ou ``None``.

    L’octet de **fin** du message (pour construire un Range HTTP) n’est pas sur cette ligne : il faut
    la ligne **suivante** du fichier trié par offset — voir :func:`contiguous_byte_range_for_record`.
    """
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    m = WGIB2_IDX_LINE_RE.match(line)
    if not m:
        return None
    try:
        rec = int(m.group(1))
        off = int(m.group(2))
    except ValueError:
        return None
    return rec, off, m.group(3)


def parse_idx_wgrib2_regex(idx_text: str) -> list[Tuple[int, int, str]]:
    """Même sortie que :func:`parse_idx_wgrib2`, en ne gardant que les lignes reconnues par regex."""
    out: list[Tuple[int, int, str]] = []
    for raw in idx_text.splitlines():
        parsed = parse_idx_line_wgrib2_regex(raw)
        if parsed is not None:
            out.append(parsed)
    return out


def contiguous_byte_range_for_record(
    idx_text: str,
    record_number: int,
    *,
    pgrb2_total_bytes: Optional[int] = None,
) -> Optional[Tuple[int, int]]:
    """
    Pour un numéro d’enregistrement du .idx, retourne ``(start, end)`` **inclusifs** pour
    :func:`get_range_bytes` (le dernier octet du message = juste avant le début du suivant,
    ou fin de fichier si dernier message — requiert ``pgrb2_total_bytes`` pour ce cas).
    """
    rows = parse_idx_wgrib2(idx_text)
    if not rows or not any(r[0] == record_number for r in rows):
        return None
    sorted_by_off = sorted(rows, key=lambda x: x[1])
    for i, (rec, off, _) in enumerate(sorted_by_off):
        if rec != record_number:
            continue
        start = off
        if i + 1 < len(sorted_by_off):
            end_inclusive = sorted_by_off[i + 1][1] - 1
        else:
            if pgrb2_total_bytes is None:
                return None
            end_inclusive = pgrb2_total_bytes - 1
        if end_inclusive < start:
            return None
        return start, end_inclusive
    return None


def byte_range_for_idx_line_containing(
    idx_text: str,
    substring: str,
    *,
    pgrb2_total_bytes: Optional[int] = None,
) -> Optional[Tuple[int, int]]:
    """
    Trouve la **première** ligne du .idx dont le texte (numéro:offset:descripteur) contient
    ``substring`` (ex. ``:TMP:2 m above ground:``), puis calcule la plage d’octets du message
    GRIB correspondant (même logique que :func:`contiguous_byte_range_for_record`).
    Retourne ``(start, end)`` inclusifs pour :func:`get_range_bytes`.
    """
    for rec, _off, rest in parse_idx_wgrib2(idx_text):
        line = f"{rec}:{_off}:{rest}"
        if substring in line:
            return contiguous_byte_range_for_record(
                idx_text, rec, pgrb2_total_bytes=pgrb2_total_bytes
            )
    return None


def byte_range_for_idx_line_descriptor_regex(
    idx_text: str,
    pattern: str | re.Pattern[str],
    *,
    flags: int = 0,
    pgrb2_total_bytes: Optional[int] = None,
) -> Optional[Tuple[int, int]]:
    """
    **Première** ligne du .idx dont le **descripteur** (partie après ``N:offset:``) correspond à
    ``pattern`` (regex). Retourne ``(start, end)`` **inclusifs** pour :func:`get_range_bytes`
    / :func:`async_get_range_bytes` (fin = octet du début du message suivant **moins un**).
    """
    pat = re.compile(pattern, flags) if isinstance(pattern, str) else pattern
    for rec, _off, rest in parse_idx_wgrib2(idx_text):
        if pat.search(rest):
            return contiguous_byte_range_for_record(
                idx_text, rec, pgrb2_total_bytes=pgrb2_total_bytes
            )
    return None


def byte_range_for_variable_and_level(
    idx_text: str,
    *,
    variable_must_contain: str,
    level_must_contain: str,
    pgrb2_total_bytes: Optional[int] = None,
) -> Optional[Tuple[int, int]]:
    """
    **Première** ligne dont le descripteur contient à la fois ``variable_must_contain`` et
    ``level_must_contain`` (ex. ``:TMP:`` et ``2 m above ground``), puis même plage qu’au‑dessus.
    """
    for rec, _off, rest in parse_idx_wgrib2(idx_text):
        if variable_must_contain in rest and level_must_contain in rest:
            return contiguous_byte_range_for_record(
                idx_text, rec, pgrb2_total_bytes=pgrb2_total_bytes
            )
    return None


def harvest_grib_chunk_by_idx_record(
    pgrb2_url: str,
    idx_text: str,
    record_number: int,
    *,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
) -> Optional[bytes]:
    """
    **Moissonneur** : après :func:`fetch_idx_text` + parsing, télécharge **uniquement** le chunk
    binaire du message ``record_number`` dans le pgrb2 (GET Range).
    """
    total = head_get_content_length(pgrb2_url, timeout_sec=timeout_sec, user_agent=user_agent)
    span = contiguous_byte_range_for_record(idx_text, record_number, pgrb2_total_bytes=total)
    if span is None:
        return None
    a, b = span
    return get_range_bytes(pgrb2_url, a, b, timeout_sec=timeout_sec, user_agent=user_agent)


def harvest_grib_chunk_by_idx_substring(
    pgrb2_url: str,
    idx_text: str,
    field_substring: str,
    *,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
) -> Optional[bytes]:
    """
    Comme :func:`harvest_grib_chunk_by_idx_record`, mais sélectionne l’enregistrement par
    sous-chaîne dans la ligne du .idx (ex. ``:TMP:2 m above ground:``).
    """
    total = head_get_content_length(pgrb2_url, timeout_sec=timeout_sec, user_agent=user_agent)
    span = byte_range_for_idx_line_containing(
        idx_text, field_substring, pgrb2_total_bytes=total
    )
    if span is None:
        return None
    a, b = span
    return get_range_bytes(pgrb2_url, a, b, timeout_sec=timeout_sec, user_agent=user_agent)


def iter_grib_message_ranges_matching_substring(
    idx_text: str,
    substring: str,
    *,
    pgrb2_total_bytes: Optional[int],
) -> Iterator[Tuple[int, int, int, str]]:
    """
    Parcourt toutes les lignes du .idx dont le descripteur contient ``substring`` et
    calcule la plage d’octets de chaque **message GRIB** correspondant.

    Retourne ``(record, start, end_inclusive, rest)`` par enregistrement.
    """
    for rec, _off, rest in parse_idx_wgrib2(idx_text):
        line = f"{rec}:{_off}:{rest}"
        if substring not in line:
            continue
        span = contiguous_byte_range_for_record(idx_text, rec, pgrb2_total_bytes=pgrb2_total_bytes)
        if span is None:
            continue
        a, b = span
        yield rec, a, b, rest


def harvest_grib_chunks_by_field_substrings(
    pgrb2_url: str,
    idx_text: str,
    field_substrings: Sequence[str],
    *,
    first_match_per_pattern: bool = True,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, Optional[bytes]]:
    """
    Télécharge **uniquement** les messages GRIB utiles (GET **Range**), directement en **RAM**
    (``bytes``) — pas le fichier pgrb2 complet.

    Pour chaque motif (sous-chaîne présente dans la ligne .idx wgrib2, ex.
    ``:TMP:2 m above ground:``, ``:APCP:surface:``) :

    - ``first_match_per_pattern=True`` (défaut) : un seul message par motif (le premier trouvé).
    - ``first_match_per_pattern=False`` : tous les messages dont la ligne contient le motif,
      concaténés en un seul ``bytes`` (fichier GRIB multi-messages — exploitable par wgrib2).

    **Limite** : un message = une grille **mondiale** (ou domaine du modèle) pour ce champ ;
    ce n’est pas une « zone ville » au sens géographique fin. Pour découper une région après
    coup : ``wgrib2`` (ex. ``-small_grib``) sur le chunk déjà en mémoire.
    """
    total = head_get_content_length(pgrb2_url, timeout_sec=timeout_sec, user_agent=user_agent)
    out: dict[str, Optional[bytes]] = {}
    for i, pat in enumerate(field_substrings):
        label = pat.strip() if len(pat) <= 96 else f"pattern_{i}"
        if first_match_per_pattern:
            span = byte_range_for_idx_line_containing(
                idx_text, pat, pgrb2_total_bytes=total
            )
            if span is None:
                out[label] = None
                continue
            a, b = span
            out[label] = get_range_bytes(
                pgrb2_url, a, b, timeout_sec=timeout_sec, user_agent=user_agent
            )
            continue
        chunks: list[bytes] = []
        for _rec, a, b, _rest in iter_grib_message_ranges_matching_substring(
            idx_text, pat, pgrb2_total_bytes=total
        ):
            chunks.append(
                get_range_bytes(
                    pgrb2_url, a, b, timeout_sec=timeout_sec, user_agent=user_agent
                )
            )
        out[label] = b"".join(chunks) if chunks else None
    return out


def fetch_idx_and_harvest_weather_fields(
    pgrb2_url: str,
    idx_url: str,
    field_substrings: Optional[Sequence[str]] = None,
    *,
    first_match_per_pattern: bool = True,
    timeout_sec: float = DEFAULT_HEAD_TIMEOUT_SEC,
    user_agent: str = DEFAULT_USER_AGENT,
    max_idx_bytes: int = MAX_IDX_FETCH_BYTES,
) -> dict[str, Optional[bytes]]:
    """
    Pipeline complet : **GET** du .idx (texte en RAM) → repères d’octets → **GET Range** sur
    le pgrb2 pour la température / précipitation (ou autres champs), **sans** télécharger le
    gros binaire entier.

    - ``field_substrings`` : si ``None``, utilise :data:`DEFAULT_FIELD_SUBSTRINGS_TEMP_PRECIP`.
    - Retour : dictionnaire ``motif → bytes`` (ou ``None`` si aucune ligne ne matche).
    """
    patterns = (
        tuple(field_substrings)
        if field_substrings is not None
        else DEFAULT_FIELD_SUBSTRINGS_TEMP_PRECIP
    )
    idx_text = fetch_idx_text(
        idx_url,
        timeout_sec=timeout_sec,
        user_agent=user_agent,
        max_bytes=max_idx_bytes,
    )
    return harvest_grib_chunks_by_field_substrings(
        pgrb2_url,
        idx_text,
        patterns,
        first_match_per_pattern=first_match_per_pattern,
        timeout_sec=timeout_sec,
        user_agent=user_agent,
    )


def _coerce_bytes_or_bytesio(buf: bytes | io.BytesIO) -> bytes:
    """Normalise ``bytes`` ou :class:`io.BytesIO` en ``bytes`` (sans fichier utilisateur)."""
    if isinstance(buf, bytes):
        return buf
    return buf.getvalue()


def _wgrib2_run_bytes(
    cmd: Sequence[str],
    raw: bytes,
    *,
    timeout_sec: float,
) -> Tuple[int, str, str]:
    """Lance wgrib2 avec les données GRIB2 sur **stdin** (binaire)."""
    p = subprocess.run(
        list(cmd),
        input=raw,
        capture_output=True,
        timeout=timeout_sec,
    )
    out = (p.stdout or b"").decode("utf-8", errors="replace")
    err = (p.stderr or b"").decode("utf-8", errors="replace")
    return p.returncode, out, err


# Motifs usuels dans la sortie texte wgrib2 (ex. ``-lon``) : ``val=285.12`` ou ``val = 1.2e-3``.
_WGRIB2_VAL_RE = re.compile(
    r"val\s*=\s*([-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?)",
    re.IGNORECASE,
)
_FLOAT_TOKEN_RE = re.compile(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?")


def parse_wgrib2_stdout_scalar(stdout: str) -> Optional[float]:
    """
    Interprète la **sortie texte** de wgrib2 (température, précip accumulée, etc.) en un seul
    ``float``.

    Stratégie : d’abord une forme ``val=…`` (typique de ``-lon``), sinon le **dernier** nombre
    plausible sur la dernière ligne non vide (secours pour CSV ou autres formats).
    """
    if not (stdout and stdout.strip()):
        return None
    m = _WGRIB2_VAL_RE.search(stdout)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        found = _FLOAT_TOKEN_RE.findall(line)
        if found:
            try:
                return float(found[-1])
            except ValueError:
                continue
    return None


def run_wgrib2_scalpel(
    grib_chunk: bytes | io.BytesIO,
    *,
    match_substring: str = ":TMP:2 m above ground:",
    wgrib2_bin: str = "wgrib2",
    timeout_sec: float = 120.0,
    extra_args: Optional[Sequence[str]] = None,
) -> Tuple[int, str, str]:
    """
    **Scalpel** : envoie le morceau GRIB2 sur **stdin** (``wgrib2 -``).

    Par défaut : ``-match`` + ``-csv <fichier>``. Beaucoup de binaires wgrib2 exigent **un chemin**
    après ``-csv`` (sinon erreur « missing arguments option=csv ») ; dans ce cas un **.csv**
    temporaire est créé, lu puis supprimé — le contenu est renvoyé comme chaîne ``stdout``
    (les messages éventuels de wgrib2 restent dans le flux processus si présents).

    Si ``extra_args`` est fourni, il est passé tel quel (à vous de fournir ``-csv`` + chemin si besoin).

    Retourne ``(code_retour, stdout, stderr)``. Interpréter les valeurs selon le champ (unités GRIB).
    """
    raw = _coerce_bytes_or_bytesio(grib_chunk)
    # Comme avant : ``None`` ou liste vide → sortie CSV par défaut.
    if extra_args is not None and len(extra_args) > 0:
        cmd = (wgrib2_bin, "-", "-match", match_substring, *list(extra_args))
        return _wgrib2_run_bytes(cmd, raw, timeout_sec=timeout_sec)

    fd, csv_path = tempfile.mkstemp(suffix=".wgrib2_out.csv")
    os.close(fd)
    try:
        cmd = [wgrib2_bin, "-", "-match", match_substring, "-csv", csv_path]
        code, proc_out, stderr = _wgrib2_run_bytes(cmd, raw, timeout_sec=timeout_sec)
        try:
            with open(csv_path, encoding="utf-8", errors="replace") as f:
                csv_text = f.read()
        except OSError:
            csv_text = ""
        # Sortie utile : souvent uniquement dans le fichier ; on préfère le CSV fichier, sinon stdout.
        out = csv_text if csv_text.strip() else proc_out
        return code, out, stderr
    finally:
        try:
            os.unlink(csv_path)
        except OSError:
            pass


def run_wgrib2_scalpel_at_point(
    grib_chunk: bytes | io.BytesIO,
    *,
    lon: float,
    lat: float,
    match_substring: str = ":TMP:2 m above ground:",
    wgrib2_bin: str = "wgrib2",
    timeout_sec: float = 120.0,
    inv_path: Optional[str] = None,
    extra_args: Optional[Sequence[str]] = None,
) -> Tuple[int, Optional[float], str, str]:
    """
    **Scalpel + point GPS** : stdin → ``wgrib2 -``, ``-match``, ``-inv`` (par défaut :attr:`os.devnull`
    pour ne pas écrire d’inventaire — sur Linux équivalent à ``/dev/null``), ``-lon LON LAT``
    (longitude puis latitude, degrés).

    Retourne ``(code_retour, valeur_float_ou_None, stdout, stderr)``. La valeur est dérivée de
    ``stdout`` via :func:`parse_wgrib2_stdout_scalar` (ex. ``val=…``). Vérifier ``code_retour``
    avant d’utiliser la valeur (en cas d’erreur wgrib2, ``stderr`` est explicite).
    """
    raw = _coerce_bytes_or_bytesio(grib_chunk)
    inv = inv_path if inv_path is not None else os.devnull
    cmd = [wgrib2_bin, "-", "-match", match_substring, "-inv", inv, "-lon", str(lon), str(lat)]
    if extra_args:
        cmd.extend(extra_args)
    code, out, err = _wgrib2_run_bytes(cmd, raw, timeout_sec=timeout_sec)
    val = parse_wgrib2_stdout_scalar(out)
    return code, val, out, err


def release_bytesio(buf: io.BytesIO) -> None:
    """
    Réinitialise un :class:`io.BytesIO` (``seek(0)`` + ``truncate(0)``) pour libérer le tampon
    entre deux membres d’ensemble (GEFS, etc.) sans attendre le ramasse-miettes.
    Les objets ``bytes`` n’ont pas besoin de cette étape : il suffit de ne plus y référencer.
    """
    buf.seek(0)
    buf.truncate(0)


def iter_gefs_member_batches(
    *,
    member_indices: Optional[Sequence[int]] = None,
    batch_size: int = GEFS_DEFAULT_MEMBER_BATCH_SIZE,
) -> Iterator[Tuple[int, ...]]:
    """
    Itère les **paquets** d’indices de membres (ex. probabilités sur les 31 sorties GEFS).

    Par défaut : ``0 … 30`` (:data:`GEFS_MEMBER_COUNT` membres), découpés en tuples d’au plus
    ``batch_size`` indices — utile pour ne jamais lancer 31 requêtes ou 31 gros buffers à la fois.
    """
    if member_indices is None:
        seq = list(range(GEFS_MEMBER_COUNT))
    else:
        seq = list(member_indices)
    if batch_size < 1:
        raise ValueError("batch_size doit être >= 1")
    for i in range(0, len(seq), batch_size):
        yield tuple(seq[i : i + batch_size])


def run_gefs_members_batched(
    process_member: Callable[[int], T],
    *,
    member_indices: Optional[Sequence[int]] = None,
    batch_size: int = GEFS_DEFAULT_MEMBER_BATCH_SIZE,
    parallel_in_batch: bool = False,
) -> list[T]:
    """
    Appelle ``process_member(member_index)`` pour chaque membre, **par lots** de ``batch_size``
    (défaut **5**) : on ne charge jamais les 31 segments en parallèle (évite le pic RAM).

    - ``parallel_in_batch=False`` (**défaut**) : séquentiel à l’intérieur du lot — RAM minimale.
    - ``parallel_in_batch=True`` : jusqu’à ``len(lot)`` threads **dans** le lot uniquement
      (micro-parallèle adapté au réseau / I/O ; toujours au plus ``batch_size`` membres actifs).

    Dans ``process_member``, après usage d’un :class:`io.BytesIO`, appeler :func:`release_bytesio`
    (ou laisser tomber la référence) **avant** de retourner, pour libérer le tampon avant le membre suivant.
    """
    results: list[T] = []
    for batch in iter_gefs_member_batches(
        member_indices=member_indices,
        batch_size=batch_size,
    ):
        if parallel_in_batch:
            from concurrent.futures import ThreadPoolExecutor

            workers = min(len(batch), batch_size)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                batch_results = list(ex.map(process_member, batch))
        else:
            batch_results = [process_member(m) for m in batch]
        results.extend(batch_results)
    return results


# Alias explicite « usine » (même implémentation que :func:`get_range_bytes`).
harvest_binary_chunk = get_range_bytes
harvest_binary_chunk_io = get_range_bytes_io


def read_local_grib_file(
    path: str | os.PathLike[str],
    *,
    max_bytes: int = 512 * 1024 * 1024,
) -> bytes:
    """
    Lit un fichier GRIB2 **local** (binaire) — même flux qu’un segment téléchargé par Range,
    mais sans réseau (tests / fichiers déjà sur disque).

    ``max_bytes`` évite de charger par erreur un pgrb2 entier multi‑Go ; augmenter si besoin.
    """
    p = os.fspath(path)
    with open(p, "rb") as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(
            f"fichier plus grand que {max_bytes} octets — tronquer ou augmenter max_bytes"
        )
    return data


def poll_until_idx_available(
    run: GfsRun,
    forecast_hours: int,
    *,
    max_wait_sec: float = 3600.0,
    on_tick: Optional[Callable[[], None]] = None,
    anti_ban: Optional[AntiBanGovernor] = None,
    use_jitter: bool = True,
) -> Optional[Tuple[str, str]]:
    """
    Boucle jusqu’à ce qu’un .idx réponde (NOMADS puis S3 à chaque itération),
    avec intervalle dynamique : rapide près de (run + 3h30), lent sinon,
    **jitter** anti-ban, et :class:`AntiBanGovernor` (403 → pause + S3 seul).

    Pour usage manuel / script ; pour du temps réel, préfère une coroutine ou un scheduler.
    """
    gov = anti_ban or AntiBanGovernor()
    deadline = time.monotonic() + max_wait_sec
    while time.monotonic() < deadline:
        now = datetime.now(timezone.utc)
        hit = probe_idx_nomads_then_s3(run, forecast_hours, anti_ban=gov)
        if gov.consume_cooldown_sleep():
            time.sleep(gov.cooldown_sec)
        if hit:
            return hit
        if on_tick:
            on_tick()
        time.sleep(gfs_poll_sleep_duration_sec(now, use_jitter=use_jitter))
    return None


def first_available_url(
    forecast_hours: int,
    *,
    now_utc: Optional[datetime] = None,
    publication_lag_hours: float = DEFAULT_PUBLICATION_LAG_HOURS,
    prefer_idx: bool = True,
    allow_pgrb2_fallback: bool = False,
) -> Optional[Tuple[str, GfsRun, str]]:
    """
    Parcourt les runs candidats (récent → ancien).

    Par défaut, le signal de départ est uniquement le **.idx** (logique
    :func:`probe_idx_hybrid`). Le pgrb2 seul n’est pas utilisé sauf si
    ``allow_pgrb2_fallback=True`` (debug / cas exceptionnel).

    Si ``prefer_idx=False`` : uniquement HEAD sur le pgrb2 (NOMADS puis S3).

    Ne télécharge pas le GRIB — uniquement des en-têtes HTTP.
    """
    for run in iter_candidate_runs(
        now_utc=now_utc,
        publication_lag_hours=publication_lag_hours,
    ):
        if prefer_idx:
            hit = probe_idx_nomads_then_s3(run, forecast_hours)
            if hit:
                url, label = hit
                return url, run, label
            if allow_pgrb2_fallback:
                hit2 = probe_pgrb2_nomads_then_s3(run, forecast_hours)
                if hit2:
                    url, label = hit2
                    return url, run, label
        else:
            hit2 = probe_pgrb2_nomads_then_s3(run, forecast_hours)
            if hit2:
                url, label = hit2
                return url, run, label
    return None


def list_urls_for_radar(
    forecast_hours: Sequence[int],
    *,
    now_utc: Optional[datetime] = None,
    publication_lag_hours: float = DEFAULT_PUBLICATION_LAG_HOURS,
    max_runs: int = 8,
) -> list[dict]:
    """
    Aide debug : pour chaque échéance demandée, liste les URLs des N derniers
    runs candidats (sans requête HTTP). Utile pour brancher un autre backend
    (Range GET, index, etc.) plus tard.
    """
    runs = list(
        iter_candidate_runs(
            now_utc=now_utc,
            publication_lag_hours=publication_lag_hours,
            max_runs=max_runs,
        )
    )
    out: list[dict] = []
    for fh in forecast_hours:
        for run in runs:
            out.append(
                {
                    "forecast_hours": fh,
                    "run": run,
                    "url": build_url_for_run(run, fh),
                    "nomads_idx": build_gfs_idx_url(run.day, run.cycle_utc, fh, base=GFS_PROD_BASE),
                    "s3_idx": build_s3_idx_url(run.day, run.cycle_utc, fh),
                    "s3_idx_candidates": list(
                        iter_s3_idx_url_candidates(run.day, run.cycle_utc, fh)
                    ),
                    "s3_pgrb2": build_s3_pgrb2_url(run.day, run.cycle_utc, fh),
                }
            )
    return out


def run_watch_loop(
    forecast_hours: int = 3,
    *,
    quiet: bool = False,
) -> None:
    """
    Boucle **infinie** : ping .idx sur le run le plus récent plausible (HEAD uniquement),
    avec intervalle sommeil/alerte + jitter + anti-ban (même logique que la prod).

    Pour arrêter : Ctrl+C. Pour lancer au boot : systemd ou cron (le script ne se « réveille »
    pas tout seul sans service OS).
    """
    gov = AntiBanGovernor()
    print(
        "Mode watch — boucle infinie (Ctrl+C pour arrêter). "
        "Intervalle ~300 s hors fenêtre, ~10 s en fenêtre intensive (+ jitter).",
        flush=True,
    )
    while True:
        now = datetime.now(timezone.utc)
        cand = list(iter_candidate_runs(now_utc=now, max_runs=1))
        if not cand:
            if not quiet:
                print(f"[{now.isoformat()}] Aucun run candidat — sleep 60s", flush=True)
            time.sleep(60.0)
            continue
        target = cand[0]
        r = probe_idx_hybrid(target, forecast_hours, anti_ban=gov)
        if gov.consume_cooldown_sleep():
            if not quiet:
                print(f"[{now.isoformat()}] Anti-ban cooldown {gov.cooldown_sec}s", flush=True)
            time.sleep(gov.cooldown_sec)
        if r:
            if not quiet:
                print(
                    f"[{now.isoformat()}] IDX OK {r.url} ({r.source}) run={target}",
                    flush=True,
                )
        elif not quiet:
            print(f"[{now.isoformat()}] Pas d'idx encore — run={target}", flush=True)
        time.sleep(gfs_poll_sleep_duration_sec(now, use_jitter=True))


def _main_local_wgrib(args: argparse.Namespace) -> None:
    """
    Test **conditions réelles** sur un fichier GRIB2 déjà présent : stdin → wgrib2
    (:func:`run_wgrib2_scalpel` ou :func:`run_wgrib2_scalpel_at_point`), sans HTTP.
    """
    path = args.grib_path
    if not path:
        raise SystemExit("mode local-wgrib : --grib-path /chemin/vers/fichier.grib2 est requis.")
    if not os.path.isfile(path):
        raise SystemExit(f"fichier introuvable : {path}")

    data = read_local_grib_file(path, max_bytes=args.max_grib_bytes)
    print(f"Lu {len(data):,} octets depuis {path!r}", flush=True)

    lon, lat = args.lon, args.lat
    if (lon is None) ^ (lat is None):
        raise SystemExit("fournir --lon et --lat ensemble, ou aucun des deux (mode -csv).")

    if lon is not None and lat is not None:
        code, val, out, err = run_wgrib2_scalpel_at_point(
            data,
            lon=float(lon),
            lat=float(lat),
            match_substring=args.match,
            timeout_sec=args.wgrib2_timeout,
        )
        print(f"\ncode retour wgrib2 : {code}", flush=True)
        print(f"valeur scalaire (parse) : {val!r}", flush=True)
        lim = args.stdout_max_chars
        if out:
            o = out if len(out) <= lim else out[:lim] + f"\n... [{len(out)} caractères au total]"
            print(f"\n--- stdout ---\n{o}", flush=True)
        if err:
            e = err if len(err) <= lim else err[:lim] + f"\n... [{len(err)} caractères au total]"
            print(f"\n--- stderr ---\n{e}", flush=True)
    else:
        code, out, err = run_wgrib2_scalpel(
            data,
            match_substring=args.match,
            timeout_sec=args.wgrib2_timeout,
        )
        print(f"\ncode retour wgrib2 : {code}", flush=True)
        lim = args.stdout_max_chars
        if out:
            o = out if len(out) <= lim else out[:lim] + f"\n... [{len(out)} caractères au total]"
            print(f"\n--- stdout (extrait) ---\n{o}", flush=True)
        if err:
            e = err if len(err) <= lim else err[:lim] + f"\n... [{len(err)} caractères au total]"
            print(f"\n--- stderr ---\n{e}", flush=True)


def _main_smoke() -> None:
    """Test unique (comportement historique) : affiche URLs + un HEAD puis quitte."""
    now = datetime.now(timezone.utc)
    print(f"UTC maintenant : {now.isoformat()}")
    for fh in (3, 24):
        print(f"\n--- Échéance +{fh} h (suffixe {forecast_suffix(fh)}) ---")
        for item in list_urls_for_radar([fh], max_runs=4)[:4]:
            print(item["url"])

    hit = first_available_url(3)
    if hit:
        u, run, label = hit
        print(f"\nPremier HEAD OK f003 : {u}\n  run={run}  source={label}")
    else:
        print("\nAucun HEAD OK pour f003 sur les derniers runs (réseau ou délai NOAA).")

    ex = GfsRun(day=now.date(), cycle_utc="12")
    print(f"\nEx. publication attendue (12z run, lag 3h30) : {run_publication_time_utc(ex).isoformat()}")

    w0, w1 = intensive_ping_window_bounds(ex)
    print(f"Fenêtre ping intensif 12z : {w0.isoformat()} → {w1.isoformat()}")

    st = gfs_poll_status()
    print(f"\nStatut polling GFS : {st}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Radar GFS NOAA (NOMADS / S3).")
    parser.add_argument(
        "mode",
        nargs="?",
        default="smoke",
        choices=("smoke", "watch", "local-wgrib"),
        help=(
            "smoke = test unique puis fin ; watch = boucle infinie (ping .idx) ; "
            "local-wgrib = tester wgrib2 sur un fichier GRIB2 local (--grib-path)."
        ),
    )
    parser.add_argument(
        "--forecast-hours",
        type=int,
        default=3,
        help="Échéance fXXX (heures), ex. 3 pour f003 (mode watch uniquement).",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Mode watch : moins de logs (erreurs / cooldown toujours utiles).",
    )
    parser.add_argument(
        "--grib-path",
        type=str,
        default=None,
        help="Mode local-wgrib : chemin vers un .grib2 / extrait GRIB déjà sur disque.",
    )
    parser.add_argument(
        "--lon",
        type=float,
        default=None,
        help="Mode local-wgrib : longitude (degrés), avec --lat → run_wgrib2_scalpel_at_point.",
    )
    parser.add_argument(
        "--lat",
        type=float,
        default=None,
        help="Mode local-wgrib : latitude (degrés), avec --lon.",
    )
    parser.add_argument(
        "--match",
        type=str,
        default=":TMP:2 m above ground:",
        help="Mode local-wgrib : sous-chaîne -match wgrib2 (défaut température 2 m).",
    )
    parser.add_argument(
        "--max-grib-bytes",
        type=int,
        default=512 * 1024 * 1024,
        help="Mode local-wgrib : taille max lue depuis le disque (défaut 512 Mo).",
    )
    parser.add_argument(
        "--wgrib2-timeout",
        type=float,
        default=120.0,
        help="Mode local-wgrib : timeout secondes pour le sous-processus wgrib2.",
    )
    parser.add_argument(
        "--stdout-max-chars",
        type=int,
        default=12000,
        help="Mode local-wgrib : tronquer stdout/stderr au-delà de ce nombre de caractères.",
    )
    args = parser.parse_args()
    if args.mode == "watch":
        run_watch_loop(forecast_hours=args.forecast_hours, quiet=args.quiet)
    elif args.mode == "local-wgrib":
        _main_local_wgrib(args)
    else:
        _main_smoke()
