#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
**Producteur de signaux trading bot1** (NOAA GFS / NOMADS + :mod:`noaa_engine`).

C’est le script invoqué par ``update_bot.sh`` pour alimenter ``finance_engine``.
Nom de fichier historique : ``run_nomads_live_test.py`` — équivalent préféré en ops :
``produce_trading_signals.py`` (simple délégation, mêmes arguments).

Réseau réel : fichiers du jour sur
https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/

Sorties (voir ``core.bot1_data_paths``) :

- ``TRADING_SIGNALS_FILE`` / manifeste / ``LAST_NOAA_RUN_FILE``
- Skip watchdog : ``trading_signals.json`` inchangé ; manifeste ``skipped_*`` +
  ``stale_policy`` — ne pas trader tant que le meta n’est pas ``ok``

``finance_engine`` refuse de trader si manifeste présent et status ≠ ``ok``, ou si
``generated_at_utc`` est trop vieux (``TRADING_SIGNALS_MAX_AGE_SEC``).

Prérequis prod : **wgrib2** dans le PATH pour scorer les marchés (sauf 0 cible).

Usage ::

    python3 bots/bot1/noaa_nomads_live/produce_trading_signals.py
    python3 bots/bot1/noaa_nomads_live/run_nomads_live_test.py
    python3 produce_trading_signals.py --forecast-hours 3 --http-debug
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import sys
import threading
import time
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

# noaa_engine vit dans le dossier voisin « Long Term »
_LONG_TERM = Path(__file__).resolve().parent.parent / "Long Term"
if str(_LONG_TERM) not in sys.path:
    sys.path.insert(0, str(_LONG_TERM))
_LONG_TERM_SCRIPTS = _LONG_TERM / "scripts"
if str(_LONG_TERM_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_LONG_TERM_SCRIPTS))
# .../POLYMARKET/bots/bot1/noaa_nomads_live/run_nomads_live_test.py -> .../POLYMARKET
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_NOMADS_DIR = Path(__file__).resolve().parent
if str(_NOMADS_DIR) not in sys.path:
    sys.path.insert(0, str(_NOMADS_DIR))

import gfs_horizons  # noqa: E402
import noaa_engine  # noqa: E402
import market_manager  # noqa: E402
from monitoring.error_guard import run_guarded  # noqa: E402

try:
    from signals.sources.model_calibrator import get_sigma_hint_for_key  # noqa: E402
except Exception:  # pragma: no cover - import minimal si signals indisponible
    get_sigma_hint_for_key = None  # type: ignore[misc, assignment]

from core.bot1_data_paths import (  # noqa: E402
    last_noaa_run_path,
    noaa_watchdog_state_path,
    trading_signals_meta_path,
    trading_signals_path,
)
from core.models import ContractSpec  # noqa: E402
from core.ensemble_prob import blend_prob, empirical_prob_from_operator, quantile  # noqa: E402

T = TypeVar("T")


def calculate_probability(delta: float, sigma: float = 1.5) -> float:
    """
    Convertit un delta (écart au seuil) en probabilité de victoire [0, 1]
    en supposant une erreur de prévision gaussienne de moyenne 0.
    """
    if sigma <= 0:
        raise ValueError("sigma doit être strictement positif")
    z = delta / (sigma * math.sqrt(2.0))
    p = 0.5 * (1.0 + math.erf(z))
    # Clamp défensif pour éviter des artefacts flottants hors [0, 1].
    return max(0.0, min(1.0, p))


def _sigma_base_for_target(target: market_manager.MarketTarget) -> float:
    """
    σ de base (sans ajustement horizon) : env global, hint calibrateur, défauts par variable.
    """
    g = os.getenv("BOT1_PROB_SIGMA", "").strip()
    if g:
        return max(1e-6, float(g))
    if os.getenv("BOT1_USE_BIAS_STD_FOR_SIGMA", "").strip().lower() in ("1", "true", "yes", "on"):
        if get_sigma_hint_for_key is not None:
            for key in (target.station_code, target.market_id):
                hint = get_sigma_hint_for_key(key)
                if hint is not None:
                    return max(1e-6, float(hint))
    v = (target.variable or "").lower()
    if v == "precip":
        return max(1e-6, float(os.getenv("BOT1_PROB_SIGMA_PRECIP", "1.5")))
    return max(1e-6, float(os.getenv("BOT1_PROB_SIGMA_TEMPS", "1.5")))


def _sigma_horizon_multiplier(horizon_hours: float) -> float:
    """
    Augmente σ quand l’échéance GFS (heures depuis la run) est grande — courbe p_win plus plate.

    Délègue à :func:`gfs_horizons.sigma_horizon_multiplier` avec env :
    ``BOT1_SIGMA_HORIZON_REF_H`` (défaut 24), ``BOT1_SIGMA_HORIZON_SLOPE`` (défaut 0,5).

    Désactiver : ``BOT1_DISABLE_SIGMA_HORIZON=1``.
    """
    if os.getenv("BOT1_DISABLE_SIGMA_HORIZON", "").strip().lower() in ("1", "true", "yes", "on"):
        return 1.0
    return gfs_horizons.sigma_horizon_multiplier(
        horizon_hours,
        ref_hours=float(os.getenv("BOT1_SIGMA_HORIZON_REF_H", "24")),
        slope=float(os.getenv("BOT1_SIGMA_HORIZON_SLOPE", "0.5")),
    )


def _resolve_sigma_for_target(
    target: market_manager.MarketTarget,
    *,
    horizon_hours: float,
) -> tuple[float, float, float]:
    """
    Retourne ``(sigma_effectif, sigma_base, mult_horizon)`` pour ``calculate_probability``.

    ``horizon_hours`` : en pratique ``max(fh)`` des tranches GFS utilisées pour ce marché.
    """
    base = _sigma_base_for_target(target)
    mult = _sigma_horizon_multiplier(horizon_hours)
    return max(1e-6, base * mult), base, mult


def _match_substring_for_variable(variable: str) -> Optional[str]:
    v = (variable or "").lower()
    if v in ("temp_max", "temp_min", "temp_avg"):
        return ":TMP:2 m above ground:"
    if v == "precip":
        return ":APCP:surface:"
    return None


def _forecast_hours_for_target(
    fallback_fh: int,
    target: market_manager.MarketTarget,
    *,
    run_start_utc: datetime,
) -> tuple[int, ...]:
    """
    Échéances GFS (fXXX) à télécharger pour ce marché.

    - **temp_*** / **precip** : si ``target_date`` + ``timezone`` (IANA) sont renseignés,
      utilise :func:`gfs_horizons.calculate_gfs_horizons` (jour calendaire du contrat en local).
    - **precip** sans jour cible : ``fallback_fh`` seul (legacy).
    - **Autres variables** : ``fallback_fh`` seul.
    - **temp_*** sans jour cible : grille **f000–f024** (legacy).
    """
    log = logging.getLogger(__name__)
    v = (target.variable or "").lower()
    if v not in ("temp_max", "temp_min", "temp_avg", "precip"):
        return (fallback_fh,)

    legacy_day0 = (0, 3, 6, 9, 12, 15, 18, 21, 24)
    td_raw = (getattr(target, "target_date", None) or "").strip()
    tz_name = (getattr(target, "locality_tz", None) or "").strip()
    is_precip = v == "precip"

    def _legacy_fallback_temp() -> tuple[int, ...]:
        log.warning(
            "market=%s variable=%s — target_date ou timezone absent — grille GFS legacy f000–f024",
            target.market_id,
            v,
        )
        return legacy_day0

    def _legacy_fallback_precip() -> tuple[int, ...]:
        log.warning(
            "market=%s precip — target_date ou timezone absent — f%03d seul (legacy)",
            target.market_id,
            fallback_fh,
        )
        return (fallback_fh,)

    if not td_raw or not tz_name:
        return _legacy_fallback_precip() if is_precip else _legacy_fallback_temp()

    try:
        y, m, d = map(int, td_raw.split("-"))
        td = date_cls(y, m, d)
    except (TypeError, ValueError):
        log.warning(
            "market=%s target_date invalide %r — grille legacy",
            target.market_id,
            td_raw,
        )
        return _legacy_fallback_precip() if is_precip else _legacy_fallback_temp()

    try:
        fh_grid = gfs_horizons.calculate_gfs_horizons(
            run_start_utc,
            td,
            tz_name,
        )
    except ValueError as e:
        log.warning(
            "market=%s calculate_gfs_horizons: %s — grille legacy",
            target.market_id,
            e,
        )
        return _legacy_fallback_precip() if is_precip else _legacy_fallback_temp()

    if not fh_grid:
        log.warning(
            "market=%s grille GFS vide pour target_date=%s tz=%s run=%s — legacy",
            target.market_id,
            td_raw,
            tz_name,
            run_start_utc.isoformat(),
        )
        return _legacy_fallback_precip() if is_precip else _legacy_fallback_temp()

    log.info(
        "market=%s GFS jour cible %s (%s): f%03d → f%03d (%s tranche(s)) var=%s",
        target.market_id,
        td_raw,
        tz_name,
        fh_grid[0],
        fh_grid[-1],
        len(fh_grid),
        v,
    )
    return fh_grid


def _normalized_resolution_bucket(target: market_manager.MarketTarget) -> str:
    for raw in (target.resolution_source_gamma or "", target.resolution_source_contract or ""):
        s = (raw or "").lower()
        if "uma" in s or "optimistic" in s:
            return "uma"
        if "pyth" in s:
            return "pyth"
        if "manual" in s or "admin" in s:
            return "manual"
    return "unknown"


def _target_passes_resolution_filter(target: market_manager.MarketTarget, log: logging.Logger) -> bool:
    """
    Filtre optionnel : ``BOT1_ALLOWED_RESOLUTION_SOURCES=uma,pyth`` (vide = tout autoriser).
    ``unknown`` : si ``BOT1_TREAT_UNKNOWN_RESOLUTION_AS_UMA=1`` (défaut), compté comme ``uma``.
    """
    raw = os.getenv("BOT1_ALLOWED_RESOLUTION_SOURCES", "").strip()
    if not raw:
        return True
    allowed = frozenset(x.strip().lower() for x in raw.split(",") if x.strip())
    b = _normalized_resolution_bucket(target)
    if b == "unknown" and os.getenv("BOT1_TREAT_UNKNOWN_RESOLUTION_AS_UMA", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    ):
        b = "uma"
    if b in allowed:
        return True
    log.info(
        "market=%s ignoré (résolution %s pas dans BOT1_ALLOWED_RESOLUTION_SOURCES=%s)",
        target.market_id,
        b,
        raw,
    )
    return False


def _gfs_fh_matches_target_calendar_day(
    run_start_utc: datetime,
    fh_i: int,
    target: market_manager.MarketTarget,
) -> bool:
    """
    Garde défensive : le *valid time* (fh) doit tomber sur ``target_date`` en local.
    Désactiver : ``BOT1_GFS_VALID_DATE_GATE=0``.
    """
    if os.getenv("BOT1_GFS_VALID_DATE_GATE", "1").strip().lower() in ("0", "false", "no"):
        return True
    td_raw = (getattr(target, "target_date", None) or "").strip()
    tz_name = (getattr(target, "locality_tz", None) or "").strip()
    if not td_raw or not tz_name:
        return True
    try:
        y, m, d = map(int, td_raw.split("-"))
        td = date_cls(y, m, d)
    except (TypeError, ValueError):
        return True
    try:
        ld = gfs_horizons.local_date_for_gfs_fh(run_start_utc, fh_i, tz_name)
    except ValueError:
        return False
    return ld == td


def _retry(name: str, fn: Callable[[], T], *, tries: int = 3, base_sleep: float = 0.8) -> T:
    last_exc: Optional[Exception] = None
    for i in range(1, tries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if i >= tries:
                break
            time.sleep(base_sleep * (2 ** (i - 1)))
    assert last_exc is not None
    raise last_exc


def _is_transient_http_error(exc: BaseException) -> bool:
    s = str(exc).lower()
    return any(
        tok in s
        for tok in (
            "http error 403",
            "http error 429",
            "http error 502",
            "http error 503",
            "http error 504",
            "timed out",
            "timeout",
            "temporarily unavailable",
        )
    )


class _GefsRequestGuard:
    """
    Garde-fou anti-ban pour la branche GEFS:
    - limite débit global (req/min) même en mode multi-thread batch
    - circuit-breaker sur erreurs HTTP transientes (403/429/5xx)
    - cooldown avant reprise
    """

    def __init__(self) -> None:
        try:
            rpm = float(os.getenv("BOT1_GEFS_MAX_REQ_PER_MIN", "60") or "60")
        except ValueError:
            rpm = 60.0
        self._min_interval_sec = 60.0 / max(1.0, rpm)
        try:
            self._cooldown_sec = float(os.getenv("BOT1_GEFS_CIRCUIT_COOLDOWN_SEC", "180") or "180")
        except ValueError:
            self._cooldown_sec = 180.0
        try:
            self._trip_after = int(os.getenv("BOT1_GEFS_CIRCUIT_BREAKER_ERRORS", "3") or "3")
        except ValueError:
            self._trip_after = 3
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0
        self._cooldown_until = 0.0
        self._consecutive_errors = 0
        self.requests_total = 0
        self.errors_total = 0
        self.cooldowns_triggered = 0
        self.sleep_total_sec = 0.0

    def before_request(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._cooldown_until:
                sleep_s = self._cooldown_until - now
                self.sleep_total_sec += sleep_s
            else:
                sleep_s = max(0.0, self._next_allowed_at - now)
                self._next_allowed_at = max(now, self._next_allowed_at) + self._min_interval_sec
                self.requests_total += 1
        if sleep_s > 0:
            time.sleep(sleep_s)
        with self._lock:
            now2 = time.monotonic()
            if now2 < self._cooldown_until:
                # encore en cooldown (réveil anticipé improbable) => attendre le reste
                rem = self._cooldown_until - now2
                self.sleep_total_sec += rem
                time.sleep(rem)

    def after_success(self) -> None:
        with self._lock:
            self._consecutive_errors = 0

    def after_error(self, exc: BaseException) -> None:
        if not _is_transient_http_error(exc):
            return
        with self._lock:
            self.errors_total += 1
            self._consecutive_errors += 1
            if self._consecutive_errors >= self._trip_after:
                self._cooldown_until = max(self._cooldown_until, time.monotonic() + self._cooldown_sec)
                self._consecutive_errors = 0
                self.cooldowns_triggered += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "gefs_req_total": self.requests_total,
            "gefs_error_total": self.errors_total,
            "gefs_cooldowns": self.cooldowns_triggered,
            "gefs_sleep_sec": round(self.sleep_total_sec, 3),
            "gefs_min_interval_sec": round(self._min_interval_sec, 3),
        }


def _convert_value_to_contract_unit(*, variable: str, unit: str, raw_value: float) -> float:
    """
    Conversion stricte des unités NOAA/NOMADS -> unité contrat.
    - TMP: NOAA renvoie Kelvin
    - APCP: NOAA renvoie kg/m² = mm
    """
    var = (variable or "").lower()
    u = (unit or "").lower()
    if var in ("temp_max", "temp_min", "temp_avg"):
        # raw_value = Kelvin
        if u in ("c", "celsius", "degc", "°c"):
            return raw_value - 273.15
        if u in ("f", "fahrenheit", "degf", "°f"):
            return (raw_value - 273.15) * 9.0 / 5.0 + 32.0
        if u in ("k", "kelvin"):
            return raw_value
        raise ValueError(f"Unité température non supportée: {unit!r}")
    if var == "precip":
        # raw_value = mm (équivalent kg/m²)
        if u in ("mm",):
            return raw_value
        if u in ("cm",):
            return raw_value / 10.0
        if u in ("m", "meter", "meters"):
            return raw_value / 1000.0
        if u in ("in", "inch", "inches"):
            return raw_value / 25.4
        raise ValueError(f"Unité précipitation non supportée: {unit!r}")
    # fallback: aucune conversion
    return raw_value


def _build_contract_spec_from_target(target: market_manager.MarketTarget) -> ContractSpec:
    rs = _normalized_resolution_bucket(target)
    if rs == "unknown" and os.getenv("BOT1_TREAT_UNKNOWN_RESOLUTION_AS_UMA", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    ):
        rs = "uma"
    end_iso = (getattr(target, "end_date_iso", None) or "").strip() or (
        (getattr(target, "end_date_utc_iso", None) or "").strip().split("T")[0]
        if getattr(target, "end_date_utc_iso", None)
        else ""
    )
    return ContractSpec(
        market_id=target.market_id,
        condition_id=target.market_id,
        question=target.question or "",
        weather_variable=target.variable,
        threshold_value=target.threshold,
        threshold_unit=target.unit,
        threshold_op=target.operator,
        threshold_range=target.threshold_range,
        oracle_station_id=target.station_code,
        oracle_source=(target.oracle_source or "unknown").strip().lower() or "unknown",
        oracle_rounding=target.oracle_rounding,
        city=(target.city or "").strip(),
        country=(target.country or "").strip(),
        end_date_iso=end_iso,
        resolution_source=rs,
    )


def _aggregate_observed_value(raw_values: list[float], target: market_manager.MarketTarget) -> float:
    """
    Construit la valeur observée à donner à ContractSpec.evaluate.
    - temp_max : max
    - temp_min : min
    - temp_avg : moyenne
    - precip   : somme (accumulation)
    - fallback : dernière valeur
    """
    if not raw_values:
        raise ValueError("Aucune valeur brute à agréger")
    v = (target.variable or "").lower()
    if v == "temp_max":
        return max(raw_values)
    if v == "temp_min":
        return min(raw_values)
    if v == "temp_avg":
        return sum(raw_values) / len(raw_values)
    if v == "precip":
        return sum(raw_values)
    return raw_values[-1]


def _gefs_member_indices() -> tuple[int, ...]:
    raw = (os.getenv("BOT1_GEFS_MEMBER_INDICES") or "").strip()
    if not raw:
        return tuple(range(noaa_engine.GEFS_MEMBER_COUNT))
    out: list[int] = []
    for tok in raw.split(","):
        t = tok.strip()
        if not t:
            continue
        if "-" in t:
            a, b = t.split("-", 1)
            ia, ib = int(a), int(b)
            step = 1 if ia <= ib else -1
            out.extend(range(ia, ib + step, step))
        else:
            out.append(int(t))
    # dédup en conservant l'ordre
    seen: set[int] = set()
    uniq: list[int] = []
    for i in out:
        if i in seen:
            continue
        seen.add(i)
        uniq.append(i)
    return tuple(uniq)


def _build_gefs_member_urls(
    day: object,
    cycle_utc: object,
    fh_i: int,
    member_idx: int,
) -> list[tuple[str, str, str]]:
    """
    Retourne une liste de candidats ``(idx_url, pgrb2_url, label)`` pour un membre GEFS.
    """
    d = str(day).strip()
    c = int(cycle_utc)
    fhs = f"{int(fh_i):03d}"
    bases_raw = (
        os.getenv("BOT1_GEFS_BASE_URLS")
        or (
            "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod/,"
            "https://noaa-gefs-pds.s3.amazonaws.com/"
        )
    )
    bases = [b.strip().rstrip("/") + "/" for b in bases_raw.split(",") if b.strip()]
    if member_idx == 0:
        ids = ("gec00", "gep00")
    else:
        ids = (f"gep{member_idx:02d}",)
    out: list[tuple[str, str, str]] = []
    for base in bases:
        for mid in ids:
            p = (
                f"{base}gefs.{d}/{c:02d}/atmos/pgrb2ap5/"
                f"{mid}.t{c:02d}z.pgrb2a.0p50.f{fhs}"
            )
            out.append((p + ".idx", p, f"{base}|{mid}"))
    return out


def _fetch_gefs_member_chunk(
    *,
    day: object,
    cycle_utc: object,
    fh_i: int,
    member_idx: int,
    match_sub: str,
    max_chunk: int,
    log: logging.Logger,
    guard: _GefsRequestGuard,
) -> bytes | None:
    """
    Télécharge le chunk GRIB d’un membre GEFS pour le champ ciblé.
    """
    for idx_url, pgrb2_url, label in _build_gefs_member_urls(day, cycle_utc, fh_i, member_idx):
        try:
            guard.before_request()
            idx_text = _retry(
                f"gefs fetch_idx_text m{member_idx} f{fh_i:03d}",
                lambda idx_url=idx_url: noaa_engine.fetch_idx_text(idx_url),
                tries=2,
            )
            guard.after_success()
            guard.before_request()
            total_i = noaa_engine.head_get_content_length(pgrb2_url)
            guard.after_success()
            span_i = noaa_engine.byte_range_for_idx_line_containing(
                idx_text, match_sub, pgrb2_total_bytes=total_i
            )
            if span_i is None:
                continue
            a_i, b_i = span_i
            if (b_i - a_i + 1) > max_chunk:
                continue
            guard.before_request()
            out = _retry(
                f"gefs get_range_bytes m{member_idx} f{fh_i:03d}",
                lambda pgrb2_url=pgrb2_url, a_i=a_i, b_i=b_i: noaa_engine.get_range_bytes(
                    pgrb2_url, a_i, b_i
                ),
                tries=2,
            )
            guard.after_success()
            return out
        except Exception as exc:
            guard.after_error(exc)
            continue
    log.debug("GEFS introuvable membre=%s f%03d", member_idx, fh_i)
    return None


def _configure_logging(*, http_debug: bool) -> logging.Logger:
    """Console : DEBUG massif + option trace HTTP bas niveau (http.client)."""
    fmt = "%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(level=logging.DEBUG, format=fmt, datefmt=datefmt, force=True)

    log = logging.getLogger("nomads_live_test")
    log.info("racine script = %s", Path(__file__).resolve().parent)
    log.info("noaa_engine chargé depuis = %s", Path(noaa_engine.__file__).resolve())
    log.debug("sys.path[0] (Long Term) = %s", _LONG_TERM)

    # Bruit utile : constantes réseau du module
    log.debug("BASE_URL_PRIORITY = %s", noaa_engine.BASE_URL_PRIORITY)
    log.debug("BASE_URL_BACKUP   = %s", noaa_engine.BASE_URL_BACKUP)
    log.debug("DEFAULT_HEAD_TIMEOUT_SEC = %s", noaa_engine.DEFAULT_HEAD_TIMEOUT_SEC)

    if http_debug:
        try:
            import http.client as http_client

            http_client.HTTPConnection.debuglevel = 1
            log.warning("HTTP debuglevel=1 activé (très verbeux sur stderr)")
        except Exception as e:
            log.warning("Impossible d'activer http.client debug : %s", e)

    return log


def _load_watchdog_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_watchdog_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _next_publication_iso(poll_status: dict[str, Any]) -> Optional[str]:
    np = poll_status.get("next_publication")
    if not isinstance(np, dict):
        return None
    at = np.get("at_utc")
    return str(at).strip() if isinstance(at, str) and at.strip() else None


def _interval_next_eligible_utc(last_ts: float, min_sec: int) -> str:
    return datetime.fromtimestamp(
        last_ts + max(0, int(min_sec)), tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_last_noaa_run(payload: dict[str, Any]) -> None:
    """Journal latéral ops (skip, prochaine fenêtre, etc.), distinct du manifeste finance."""
    base = {"at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    base.update(payload)
    _write_json_atomic(last_noaa_run_path(), base)


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _save_trading_signals(path: Path, payload: dict) -> None:
    _write_json_atomic(path, payload)


def _write_signals_meta(
    *,
    status: str,
    reason: str,
    forecast_cycle_utc: str | None,
    signals_written: int | None,
    targets_expected: int | None,
    wgrib2_available: bool | None,
    pipeline_exit_code: int,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """État du dernier run NOAA pour downstream (finance_engine). status ok = signaux fiables."""
    payload: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forecast_cycle_utc": forecast_cycle_utc,
        "status": status,
        "reason": reason,
        "signals_written": signals_written,
        "targets_expected": targets_expected,
        "wgrib2_available": wgrib2_available,
        "pipeline_exit_code": pipeline_exit_code,
    }
    if extra:
        payload.update(extra)
    _write_json_atomic(trading_signals_meta_path(), payload)


def _format_forecast_cycle_utc(day: object, cycle_utc: object) -> str:
    """
    Formate le cycle GFS en timestamp UTC stable pour downstream.
    Ex: day=20260324, cycle_utc=6 -> 2026-03-24T06:00:00Z
    """
    day_raw = str(day).strip()
    if len(day_raw) == 8 and day_raw.isdigit():
        day_fmt = f"{day_raw[0:4]}-{day_raw[4:6]}-{day_raw[6:8]}"
    else:
        day_fmt = day_raw
    try:
        cyc = int(cycle_utc)
    except (TypeError, ValueError):
        cyc = 0
    return f"{day_fmt}T{cyc:02d}:00:00Z"


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "Producteur de signaux bot1: NOMADS GFS + noaa_engine + wgrib2 "
            "(logs detailles en DEBUG)."
        ),
    )
    p.add_argument("--forecast-hours", type=int, default=3, help="Echeance fXXX (ex. 3 -> f003).")
    p.add_argument(
        "--max-chunk-mb",
        type=float,
        default=15.0,
        help="Plafond taille segment GRIB (Mo) avant de skip le GET Range.",
    )
    p.add_argument(
        "--http-debug",
        action="store_true",
        help="Active http.client.HTTPConnection.debuglevel=1 (très verbeux).",
    )
    p.add_argument(
        "--skip-wgrib2",
        action="store_true",
        help="Ne pas invoquer wgrib2 même si présent.",
    )
    p.add_argument(
        "--min-run-interval-sec",
        type=int,
        default=900,
        help="Intervalle minimal entre 2 runs complets (défaut 900s).",
    )
    p.add_argument(
        "--allow-outside-window",
        action="store_true",
        help="Autorise l'exécution hors fenêtre GFS intensive (debug).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Ignore les garde-fous watchdog (debug uniquement).",
    )
    args = p.parse_args()

    log = _configure_logging(http_debug=args.http_debug)
    fh = max(0, int(args.forecast_hours))
    max_chunk = int(args.max_chunk_mb * 1024 * 1024)
    state_path = noaa_watchdog_state_path()
    now_ts = time.time()
    status = noaa_engine.gfs_poll_status()
    mode = str(status.get("mode", "sleep"))

    if not args.force:
        if (not args.allow_outside_window) and mode != "alert":
            log.warning(
                "Watchdog: hors fenêtre intensive GFS (mode=%s), run NOAA ignoré.",
                mode,
            )
            _write_signals_meta(
                status="skipped_watchdog_window",
                reason=f"mode_gfs={mode!r} (hors fenêtre intensive); pas de nouveaux signaux",
                forecast_cycle_utc=None,
                signals_written=None,
                targets_expected=None,
                wgrib2_available=bool(shutil.which("wgrib2")),
                pipeline_exit_code=0,
                extra={
                    "trading_signals_json_unchanged": True,
                    "stale_policy": "refuse_trade_until_meta_status_ok",
                },
            )
            _write_last_noaa_run(
                {
                    "skipped": True,
                    "meta_status": "skipped_watchdog_window",
                    "reason": f"mode_gfs={mode!r}",
                    "next_eligible_utc": _next_publication_iso(status),
                    "trading_signals_json_unchanged": True,
                }
            )
            return 0
        st = _load_watchdog_state(state_path)
        last_ts = float(st.get("last_run_started_ts", 0.0) or 0.0)
        since = now_ts - last_ts
        if last_ts > 0 and since < max(0, int(args.min_run_interval_sec)):
            log.warning(
                "Watchdog: run ignoré (dernière exécution il y a %.1fs, min=%ss).",
                since,
                int(args.min_run_interval_sec),
            )
            _write_signals_meta(
                status="skipped_watchdog_interval",
                reason=(
                    f"min_run_interval_sec={int(args.min_run_interval_sec)} "
                    f"non écoulé (dernière exécution il y a {since:.1f}s)"
                ),
                forecast_cycle_utc=None,
                signals_written=None,
                targets_expected=None,
                wgrib2_available=bool(shutil.which("wgrib2")),
                pipeline_exit_code=0,
                extra={
                    "trading_signals_json_unchanged": True,
                    "stale_policy": "refuse_trade_until_meta_status_ok",
                },
            )
            _write_last_noaa_run(
                {
                    "skipped": True,
                    "meta_status": "skipped_watchdog_interval",
                    "reason": f"min_interval_sec={int(args.min_run_interval_sec)}",
                    "next_eligible_utc": _interval_next_eligible_utc(
                        last_ts, int(args.min_run_interval_sec)
                    ),
                    "trading_signals_json_unchanged": True,
                }
            )
            return 0

    _save_watchdog_state(
        state_path,
        {
            "last_run_started_ts": now_ts,
            "last_run_started_utc": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
        },
    )

    # Vérification liaison JSON <-> SQLite (registre de marchés actifs)
    try:
        markets_json = market_manager.default_markets_json_path()
        stations_db = market_manager.default_master_stations_db_path()
        if not stations_db.is_file() or stations_db.stat().st_size == 0:
            log.error("SQLite stations manquante ou vide: %s", stations_db)
            log.error(
                "Lance d'abord: python3 \"bots/bot1/Long Term/scripts/sync_master_stations.py\""
            )
            _write_signals_meta(
                status="error",
                reason="SQLite stations manquante ou vide (sync_master_stations requis)",
                forecast_cycle_utc=None,
                signals_written=None,
                targets_expected=None,
                wgrib2_available=bool(shutil.which("wgrib2")),
                pipeline_exit_code=1,
            )
            return 1
        loaded = market_manager.load_active_markets(markets_json)
        targets_all = market_manager.build_market_targets(markets_json, stations_db)
        targets = [t for t in targets_all if _target_passes_resolution_filter(t, log)]
        if len(targets) < len(targets_all):
            log.info(
                "Filtre résolution Polymarket: %s → %s cible(s)",
                len(targets_all),
                len(targets),
            )
        georef_ok = len(targets_all)
        miss = len(loaded) - georef_ok
        log.info(
            "Liaison registre: markets=%s, stations_résolues=%s, sans_coord=%s, "
            "cibles_après_filtre_résolution=%s",
            len(loaded),
            georef_ok,
            miss,
            len(targets),
        )
        for t in targets[:10]:
            log.debug(
                "  target market=%s station=%s source=%s var=%s unit=%s op=%s thr=%s lat=%.5f lon=%.5f",
                t.market_id,
                t.station_code,
                t.oracle_source,
                t.variable,
                t.unit,
                t.operator,
                t.threshold,
                t.latitude,
                t.longitude,
            )
        if not targets:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if targets_all:
                log.info(
                    "0 cible après filtre résolution (BOT1_ALLOWED_RESOLUTION_SOURCES) — signaux vides, meta ok."
                )
                empty_reason = "Cibles géoréférencées mais toutes exclues par filtre résolution Polymarket"
            else:
                log.info("Aucune cible marché (registre vide ou stations introuvables) — signaux vides, meta ok.")
                empty_reason = "Aucun marché actif avec station résolue"
            signals_path = trading_signals_path()
            _save_trading_signals(
                signals_path,
                {"last_run_utc": ts, "forecast_cycle_utc": "", "signals": []},
            )
            _write_signals_meta(
                status="ok",
                reason=empty_reason,
                forecast_cycle_utc=None,
                signals_written=0,
                targets_expected=0,
                wgrib2_available=bool(shutil.which("wgrib2")),
                pipeline_exit_code=0,
                extra={
                    "trading_signals_json_unchanged": False,
                    "stale_policy": "ok",
                },
            )
            _write_last_noaa_run(
                {
                    "skipped": False,
                    "meta_status": "ok",
                    "forecast_cycle_utc": None,
                    "signals_written": 0,
                    "trading_signals_json_unchanged": False,
                }
            )
            return 0
    except Exception:
        log.exception("Echec liaison JSON <-> SQLite")
        _write_signals_meta(
            status="error",
            reason="Echec liaison active_markets.json / master_stations.sqlite3",
            forecast_cycle_utc=None,
            signals_written=None,
            targets_expected=None,
            wgrib2_available=bool(shutil.which("wgrib2")),
            pipeline_exit_code=1,
        )
        return 1

    t0 = time.monotonic()
    now = datetime.now(timezone.utc)
    log.info("========== DÉBUT TEST NOMADS ==========")
    log.info("UTC = %s", now.isoformat())
    log.info("forecast_hours = %s (f%03d)", fh, fh)

    hit = None
    chosen_run = None
    for run in noaa_engine.iter_candidate_runs(now_utc=now, max_runs=24):
        log.info("— candidat run day=%s cycle_utc=%s", run.day, run.cycle_utc)
        idx_nomads = noaa_engine.build_gfs_idx_url(
            run.day, run.cycle_utc, fh, base=noaa_engine.BASE_URL_PRIORITY
        )
        log.info("  URL .idx NOMADS : %s", idx_nomads)

        t_probe = time.monotonic()
        hit = noaa_engine.probe_idx_hybrid(run, fh)
        log.debug("  probe_idx_hybrid durée ≈ %.3fs", time.monotonic() - t_probe)

        if hit:
            chosen_run = run
            log.info(
                "  OK probe — source=%s http=%s url=%s last_modified=%s",
                hit.source,
                hit.status_code,
                hit.url,
                hit.last_modified,
            )
            break
        log.warning("  probe_idx_hybrid → None, essai suivant")

    if not hit or chosen_run is None:
        log.error("Aucun .idx trouvé (NOMADS/S3) sur les runs candidats.")
        _write_signals_meta(
            status="error_no_idx",
            reason="Aucun fichier .idx GFS disponible sur les runs candidats (NOMADS/S3)",
            forecast_cycle_utc=None,
            signals_written=None,
            targets_expected=len(targets),
            wgrib2_available=bool(shutil.which("wgrib2")),
            pipeline_exit_code=1,
        )
        return 1

    pgrb2_url = noaa_engine.build_gfs_pgrb2_url(
        chosen_run.day, chosen_run.cycle_utc, fh, base=noaa_engine.GFS_PROD_BASE
    )
    log.info("URL pgrb2 associée : %s", pgrb2_url)

    try:
        log.info("— Étape GET .idx (texte)")
        t1 = time.monotonic()
        idx_text = noaa_engine.fetch_idx_text(hit.url)
        log.info("  fetch_idx_text : %.3fs, %s caractères", time.monotonic() - t1, len(idx_text))
        lines = idx_text.splitlines()
        log.info("  lignes = %s", len(lines))
        log.debug("  [extrait 12 premières lignes]\n%s", "\n".join(lines[:12]))

        log.info("— Étape parse_idx_wgrib2")
        parsed = noaa_engine.parse_idx_wgrib2(idx_text)
        log.info("  enregistrements = %s", len(parsed))
        if parsed:
            log.debug("  [0] = %s", parsed[0])
            log.debug("  [-1] = %s", parsed[-1])

        log.info("— Étape HEAD Content-Length (pgrb2)")
        t2 = time.monotonic()
        total = noaa_engine.head_get_content_length(pgrb2_url)
        log.info("  head_get_content_length : %.3fs → %s", time.monotonic() - t2, total)

        log.info("— Étape byte_range TMP 2 m")
        needed_match_substrings = {
            ms for ms in (_match_substring_for_variable(t.variable) for t in targets) if ms
        }
        if not needed_match_substrings:
            log.error("Aucun champ NOAA supporté par les targets (variables non mappées).")
            _write_signals_meta(
                status="error",
                reason="Variables marchés non mappées vers champs NOAA (TMP/APCP)",
                forecast_cycle_utc=_format_forecast_cycle_utc(chosen_run.day, chosen_run.cycle_utc),
                signals_written=None,
                targets_expected=len(targets),
                wgrib2_available=bool(shutil.which("wgrib2")),
                pipeline_exit_code=1,
            )
            return 1

        # Correction RAM: vérifie la taille via offsets AVANT tout GET Range.
        span_by_match: dict[str, tuple[int, int]] = {}
        available_chunks: dict[str, bytes] = {}
        for m in sorted(needed_match_substrings):
            span = noaa_engine.byte_range_for_idx_line_containing(
                idx_text,
                m,
                pgrb2_total_bytes=total,
            )
            if span is None:
                log.warning("  champ NOAA introuvable dans idx: %s", m)
                continue
            a, b = span
            nbytes = b - a + 1
            span_by_match[m] = span
            log.info("  span %s => %s..%s (%s octets ~%.3f Mo)", m, a, b, nbytes, nbytes / (1024 * 1024))
            if nbytes > max_chunk:
                log.warning("  span %s > plafond %s Mo — skip GET", m, args.max_chunk_mb)
                continue
            try:
                chunk = _retry(
                    f"get_range_bytes {m}",
                    lambda m=m, a=a, b=b: noaa_engine.get_range_bytes(pgrb2_url, a, b),
                    tries=3,
                )
            except Exception as exc:
                log.warning("  GET range échoué pour %s: %s", m, exc)
                continue
            available_chunks[m] = chunk
            log.info("  chunk %s récupéré: %s octets", m, len(chunk))
        log.info("  champs NOAA demandés=%s, disponibles=%s", len(needed_match_substrings), len(available_chunks))
        if not available_chunks:
            log.warning("Aucun chunk NOAA disponible après contrôles mémoire/réseau.")
            log.info("========== FIN TEST (partiel) en %.2fs ==========", time.monotonic() - t0)
            _write_signals_meta(
                status="error_no_chunks",
                reason="Aucun segment GRIB récupérable (taille/plage idx/réseau)",
                forecast_cycle_utc=_format_forecast_cycle_utc(chosen_run.day, chosen_run.cycle_utc),
                signals_written=None,
                targets_expected=len(targets),
                wgrib2_available=bool(shutil.which("wgrib2")),
                pipeline_exit_code=1,
            )
            return 1

        wgrib_code: int | None = None
        wgrib_exe = None if args.skip_wgrib2 else shutil.which("wgrib2")
        if args.skip_wgrib2:
            log.info("— wgrib2 ignoré (--skip-wgrib2)")
        elif not wgrib_exe:
            log.error(
                "wgrib2 absent du PATH — impossible de scorer les marchés. "
                "Installez wgrib2 (ex. paquet wgrib2 / conda) et vérifiez le PATH."
            )
        if len(targets) > 0 and not wgrib_exe:
            _write_signals_meta(
                status="skipped_no_wgrib2",
                reason=(
                    "wgrib2 requis pour extraire les valeurs au point; "
                    "absent du PATH ou désactivé via --skip-wgrib2"
                ),
                forecast_cycle_utc=_format_forecast_cycle_utc(chosen_run.day, chosen_run.cycle_utc),
                signals_written=None,
                targets_expected=len(targets),
                wgrib2_available=False,
                pipeline_exit_code=1,
            )
            log.info("========== FIN ÉCHEC (wgrib2 requis) en %.2fs ==========", time.monotonic() - t0)
            return 1

        log.info(
            "— Étape run_wgrib2_scalpel_at_point (boucle dynamique sur targets) — %s",
            wgrib_exe,
        )
        failed_targets = 0
        compared_targets = 0
        trading_signals: list[dict] = []
        gefs_guard = _GefsRequestGuard()
        for target in targets:
            match_sub = _match_substring_for_variable(target.variable)
            if not match_sub:
                failed_targets += 1
                log.warning(
                    "  market=%s station=%s variable non supportée=%s",
                    target.market_id,
                    target.station_code,
                    target.variable,
                )
                continue
            chunk = available_chunks.get(match_sub)
            if not chunk:
                failed_targets += 1
                log.warning(
                    "  market=%s station=%s champ NOAA manquant pour variable=%s (%s)",
                    target.market_id,
                    target.station_code,
                    target.variable,
                    match_sub,
                )
                continue
            t4 = time.monotonic()
            run_start_utc = gfs_horizons.gfs_run_start_utc_from_parts(
                chosen_run.day, chosen_run.cycle_utc
            )
            fh_series = _forecast_hours_for_target(
                fh, target, run_start_utc=run_start_utc
            )
            raw_samples: list[float] = []
            gefs_member_values: list[float] = []
            use_gefs = os.getenv("BOT1_USE_GEFS_ENSEMBLE", "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            try:
                gefs_min_members = int(os.getenv("BOT1_GEFS_MIN_VALID_MEMBERS", "20"))
            except ValueError:
                gefs_min_members = 20
            try:
                gefs_alpha = float(os.getenv("BOT1_GEFS_BLEND_ALPHA", "1.0"))
            except ValueError:
                gefs_alpha = 1.0
            try:
                gefs_batch = int(os.getenv("BOT1_GEFS_MEMBER_BATCH_SIZE", str(noaa_engine.GEFS_DEFAULT_MEMBER_BATCH_SIZE)))
            except ValueError:
                gefs_batch = noaa_engine.GEFS_DEFAULT_MEMBER_BATCH_SIZE
            gefs_parallel = os.getenv("BOT1_GEFS_PARALLEL_IN_BATCH", "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            for fh_i in fh_series:
                try:
                    pgrb2_url_i = noaa_engine.build_gfs_pgrb2_url(
                        chosen_run.day, chosen_run.cycle_utc, fh_i, base=noaa_engine.GFS_PROD_BASE
                    )
                    idx_url_i = noaa_engine.build_gfs_idx_url(
                        chosen_run.day, chosen_run.cycle_utc, fh_i, base=noaa_engine.BASE_URL_PRIORITY
                    )
                    idx_text_i = _retry(
                        f"fetch_idx_text f{fh_i:03d}",
                        lambda idx_url_i=idx_url_i: noaa_engine.fetch_idx_text(idx_url_i),
                        tries=2,
                    )
                    total_i = noaa_engine.head_get_content_length(pgrb2_url_i)
                    span_i = noaa_engine.byte_range_for_idx_line_containing(
                        idx_text_i, match_sub, pgrb2_total_bytes=total_i
                    )
                    if span_i is None:
                        continue
                    a_i, b_i = span_i
                    if (b_i - a_i + 1) > max_chunk:
                        continue
                    chunk_i = _retry(
                        f"get_range_bytes f{fh_i:03d}",
                        lambda pgrb2_url_i=pgrb2_url_i, a_i=a_i, b_i=b_i: noaa_engine.get_range_bytes(
                            pgrb2_url_i, a_i, b_i
                        ),
                        tries=2,
                    )
                except Exception:
                    continue
                code, val, out, err = _retry(
                    f"wgrib2 f{fh_i:03d}",
                    lambda chunk_i=chunk_i: noaa_engine.run_wgrib2_scalpel_at_point(
                        chunk_i,
                        lon=target.longitude,
                        lat=target.latitude,
                        match_substring=match_sub,
                        timeout_sec=120.0,
                    ),
                    tries=2,
                )
                if code == 0 and val is not None:
                    if not _gfs_fh_matches_target_calendar_day(run_start_utc, fh_i, target):
                        log.warning(
                            "  market=%s f%03d: valid date local ≠ target_date — échantillon ignoré (gate)",
                            target.market_id,
                            fh_i,
                        )
                        continue
                    raw_samples.append(val)
                elif code != 0:
                    wgrib_code = code

            # GEFS ensemble (optionnel) : p(win)=succès_membres / membres_valides.
            if use_gefs:
                member_indices = _gefs_member_indices()

                def _process_member(member_i: int) -> float | None:
                    member_raw: list[float] = []
                    for fh_gefs in fh_series:
                        if not _gfs_fh_matches_target_calendar_day(run_start_utc, fh_gefs, target):
                            continue
                        chunk_member = _fetch_gefs_member_chunk(
                            day=chosen_run.day,
                            cycle_utc=chosen_run.cycle_utc,
                            fh_i=fh_gefs,
                            member_idx=member_i,
                            match_sub=match_sub,
                            max_chunk=max_chunk,
                            log=log,
                            guard=gefs_guard,
                        )
                        if not chunk_member:
                            continue
                        code_m, val_m, _out_m, _err_m = _retry(
                            f"wgrib2 gefs m{member_i} f{fh_gefs:03d}",
                            lambda chunk_member=chunk_member: noaa_engine.run_wgrib2_scalpel_at_point(
                                chunk_member,
                                lon=target.longitude,
                                lat=target.latitude,
                                match_substring=match_sub,
                                timeout_sec=120.0,
                            ),
                            tries=2,
                        )
                        if code_m == 0 and val_m is not None:
                            member_raw.append(val_m)
                    if not member_raw:
                        return None
                    try:
                        raw_agg = _aggregate_observed_value(member_raw, target)
                        return _convert_value_to_contract_unit(
                            variable=target.variable,
                            unit=target.unit,
                            raw_value=raw_agg,
                        )
                    except Exception:
                        return None

                member_outputs = noaa_engine.run_gefs_members_batched(
                    _process_member,
                    member_indices=member_indices,
                    batch_size=max(1, gefs_batch),
                    parallel_in_batch=gefs_parallel,
                )
                gefs_member_values = [float(v) for v in member_outputs if v is not None]

            compared_targets += 1
            if not raw_samples:
                failed_targets += 1
                log.error(
                    "  market=%s station=%s -> aucune valeur NOAA exploitable",
                    target.market_id,
                    target.station_code,
                )
                continue
            try:
                raw_observed = _aggregate_observed_value(raw_samples, target)
                val_converted = _convert_value_to_contract_unit(
                    variable=target.variable,
                    unit=target.unit,
                    raw_value=raw_observed,
                )
                spec = _build_contract_spec_from_target(target)
                passed = spec.evaluate(val_converted)
            except Exception as e:
                failed_targets += 1
                log.error(
                    "  market=%s station=%s conversion/eval impossible: %s",
                    target.market_id,
                    target.station_code,
                    e,
                )
                continue
            decision = "BET_WIN" if passed else "BET_LOSE"
            delta = val_converted - target.threshold
            fh_horizon = float(max(fh_series))
            sigma_eff, sigma_base, sigma_mult = _resolve_sigma_for_target(
                target, horizon_hours=fh_horizon
            )
            pwin_sigma = calculate_probability(delta, sigma=sigma_eff)
            pwin = pwin_sigma
            pwin_gefs: float | None = None
            if use_gefs and len(gefs_member_values) >= max(1, gefs_min_members):
                pwin_gefs = empirical_prob_from_operator(
                    gefs_member_values, target.operator, target.threshold
                )
                pwin = blend_prob(pwin_gefs, pwin_sigma, alpha=gefs_alpha)
            recommended_side = "YES" if pwin >= 0.5 else "NO"
            sig_row = {
                "market_id": target.market_id,
                "station": target.station_code,
                "p_win": round(pwin, 3),
                "delta": round(delta, 2),
                "recommended_side": recommended_side,
                "prob_model": "sigma",
            }
            if pwin_gefs is not None:
                sig_row["prob_model"] = "gefs_blend" if gefs_alpha < 0.999 else "gefs_empirical"
                sig_row["p_win_sigma"] = round(pwin_sigma, 4)
                sig_row["p_win_gefs"] = round(pwin_gefs, 4)
                sig_row["ensemble_n"] = len(gefs_member_values)
                sig_row["ensemble_successes"] = int(round(pwin_gefs * len(gefs_member_values)))
                sig_row["ensemble_p10"] = round(quantile(gefs_member_values, 0.10), 3)
                sig_row["ensemble_p50"] = round(quantile(gefs_member_values, 0.50), 3)
                sig_row["ensemble_p90"] = round(quantile(gefs_member_values, 0.90), 3)
            trading_signals.append(sig_row)
            log.info(
                "  decision market_id=%s station=%s source=%s var=%s unit=%s op=%s samples=%s lat=%.5f lon=%.5f raw=%.3f conv=%.3f thr=%.3f delta=%+.3f pwin=%.4f sigma=%.3f (base=%.3f mult=%.3f fh_max=%.0f) gefs_n=%s gefs_p=%s result=%s (%.3fs)",
                target.market_id,
                target.station_code,
                target.oracle_source,
                target.variable,
                target.unit,
                target.operator,
                len(raw_samples),
                target.latitude,
                target.longitude,
                raw_observed,
                val_converted,
                target.threshold,
                delta,
                pwin,
                sigma_eff,
                sigma_base,
                sigma_mult,
                fh_horizon,
                len(gefs_member_values),
                f"{pwin_gefs:.4f}" if pwin_gefs is not None else "-",
                decision,
                time.monotonic() - t4,
            )

        log.info(
            "  synthèse comparaison: targets=%s comparés=%s erreurs=%s",
            len(targets),
            compared_targets,
            failed_targets,
        )
        fc_utc = _format_forecast_cycle_utc(chosen_run.day, chosen_run.cycle_utc)
        signals_payload = {
            "last_run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "forecast_cycle_utc": fc_utc,
            "signals": trading_signals,
        }
        signals_path = trading_signals_path()
        _save_trading_signals(signals_path, signals_payload)
        log.info(
            "  trading signals écrits: path=%s signaux=%s",
            signals_path,
            len(trading_signals),
        )

        if wgrib_code is not None and wgrib_code != 0:
            log.error("wgrib2 a retourné code=%s — échec", wgrib_code)
            log.info("========== FIN TEST ÉCHEC wgrib2 en %.2fs ==========", time.monotonic() - t0)
            _write_signals_meta(
                status="error_wgrib2",
                reason=f"wgrib2 a retourné un code d'erreur={wgrib_code}",
                forecast_cycle_utc=fc_utc,
                signals_written=len(trading_signals),
                targets_expected=len(targets),
                wgrib2_available=True,
                pipeline_exit_code=1,
            )
            return 1

        if len(targets) > 0 and not trading_signals:
            log.error("Aucun signal produit malgré %s cible(s) active(s).", len(targets))
            _write_signals_meta(
                status="error_no_signals_produced",
                reason="Boucle wgrib2 n'a produit aucun signal (toutes cibles en échec partiel)",
                forecast_cycle_utc=fc_utc,
                signals_written=0,
                targets_expected=len(targets),
                wgrib2_available=True,
                pipeline_exit_code=1,
            )
            log.info("========== FIN ÉCHEC (aucun signal) en %.2fs ==========", time.monotonic() - t0)
            return 1

        _write_signals_meta(
            status="ok",
            reason="Signaux NOAA/wgrib2 écrits pour ce cycle",
            forecast_cycle_utc=fc_utc,
            signals_written=len(trading_signals),
            targets_expected=len(targets),
            wgrib2_available=True,
            pipeline_exit_code=0,
            extra={
                "trading_signals_json_unchanged": False,
                "stale_policy": "ok",
                "gefs_network_guard": gefs_guard.as_dict(),
            },
        )
        gs = gefs_guard.as_dict()
        if (os.getenv("BOT1_USE_GEFS_ENSEMBLE", "").strip().lower() in ("1", "true", "yes", "on")):
            log.info(
                "GEFS guard: req=%s err=%s cooldowns=%s sleep=%.3fs interval=%.3fs",
                gs["gefs_req_total"],
                gs["gefs_error_total"],
                gs["gefs_cooldowns"],
                gs["gefs_sleep_sec"],
                gs["gefs_min_interval_sec"],
            )
        _write_last_noaa_run(
            {
                "skipped": False,
                "meta_status": "ok",
                "forecast_cycle_utc": fc_utc,
                "signals_written": len(trading_signals),
                "trading_signals_json_unchanged": False,
            }
        )
        log.info("========== FIN TEST OK en %.2fs ==========", time.monotonic() - t0)
        return 0
    except Exception:
        log.exception("Échec pipeline (exception ci-dessous)")
        _write_signals_meta(
            status="error_exception",
            reason="Exception non gérée dans le pipeline NOMADS (voir logs)",
            forecast_cycle_utc=None,
            signals_written=None,
            targets_expected=None,
            wgrib2_available=bool(shutil.which("wgrib2")),
            pipeline_exit_code=1,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(run_guarded("bot1.noaa_nomads_live", main))
