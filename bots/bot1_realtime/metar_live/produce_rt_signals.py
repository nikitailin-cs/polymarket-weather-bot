#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Producteur de signaux **bot 1 temps réel** : METAR NOAA (AWC) + ``icao_mapping.json``.

**Phase 3 — Science « sniper »** (pas de probabilités fines) : pour chaque marché du
mapping, dernier METAR du registre ; décision à trois états :

- **Attaque** : seuil franchi pour le côté YES → ``p_win = 1.0`` (``binary_state=attack``).
  Marché « au-dessus » : ``T ≥ seuil`` ; « en dessous » : ``T ≤ seuil``.
- **Fuite** : **heure locale actuelle** (mur) **>** deadline ET seuil **non** franchi
  → ``p_win = 0.0`` (``binary_state=flee``).
- **Standby** : sinon → ``p_win = 0.5`` (``binary_state=standby``), on attend le prochain rapport.

Sorties : ``realtime_signals.json`` et manifeste ``realtime_signals_meta.json`` avec
``generated_at_unix`` (TTL de cohérence avec ``signals_ttl_sec``).

Usage ::

    python bots/bot1_realtime/metar_live/produce_rt_signals.py
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.bot1_realtime_data_paths import (  # noqa: E402
    icao_mapping_path,
    realtime_signals_meta_path,
    realtime_signals_path,
)
from bots.bot1_realtime.metar_live.realtime_metar_registry import (  # noqa: E402
    temp_celsius_to_contract_unit,
    utc_observation_to_local,
)
from core.metar_source import get_metar_registry, selected_source  # noqa: E402
from core.rt_daily_extrema import Bucket, decide_bucket  # noqa: E402
from core.station_day_state import resolution_value  # noqa: E402

LOG = logging.getLogger("produce_rt_signals")


def _parse_hhmm(raw: str) -> tuple[int, int]:
    s = (raw or "23:50").strip()
    parts = s.replace(":", " ").split()
    if len(parts) >= 2:
        return int(parts[0]), int(parts[1])
    if len(s) == 4 and s.isdigit():
        return int(s[:2]), int(s[2:])
    return 23, 50


def _local_now(tz_name: str) -> datetime:
    name = (tz_name or "UTC").strip() or "UTC"
    try:
        return datetime.now(ZoneInfo(name))
    except Exception:
        LOG.warning("timezone invalide %r — UTC pour détection « mort »", tz_name)
        return datetime.now(timezone.utc)


def _signals_ttl_sec() -> int:
    try:
        return max(0, int(float(os.getenv("REALTIME_SIGNALS_MAX_AGE_SEC", "180"))))
    except ValueError:
        return 180


def _sniper_p_win(
    *,
    temp_contract: float,
    threshold: float,
    above: bool,
    tz_name: str,
    deadline_hhmm: str,
) -> tuple[float, str, float]:
    """
    Retourne (p_win, binary_state, gap_unmet).

    - ``attack`` : seuil franchi pour YES → ``p_win=1.0``.
    - ``flee`` : heure locale **actuelle** (mur) après deadline et seuil non franchi → ``0.0``.
    - ``standby`` : sinon → ``0.5``.

    ``gap`` : degrés restants pour atteindre le seuil (côté « au-dessus » ou « en dessous »).
    """
    if above:
        if temp_contract >= threshold:
            return 1.0, "attack", 0.0
        gap = threshold - temp_contract
    else:
        if temp_contract <= threshold:
            return 1.0, "attack", 0.0
        gap = temp_contract - threshold

    hh, mm = _parse_hhmm(deadline_hhmm)
    local = _local_now(tz_name)
    after_deadline = (local.hour, local.minute) >= (hh, mm)
    if after_deadline:
        if above and temp_contract < threshold:
            return 0.0, "flee", gap
        if not above and temp_contract > threshold:
            return 0.0, "flee", gap
    return 0.5, "standby", gap


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _validate_market_spec(spec: dict[str, Any], market_id: str) -> None:
    """Champs requis alignés sur ``data/icao_mapping.schema.json``."""
    for key in ("icao", "threshold", "timezone"):
        if key not in spec:
            raise ValueError(
                f"marché {market_id!r}: champ requis {key!r} "
                "(voir data/icao_mapping.schema.json)"
            )


def _load_icao_mapping(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"icao_mapping introuvable: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("icao_mapping.json doit être un objet JSON à la racine")
    out: dict[str, dict[str, Any]] = {}
    for market_id, spec in raw.items():
        if not isinstance(market_id, str) or not market_id.strip():
            continue
        if market_id.startswith("_"):
            continue
        if not isinstance(spec, dict):
            LOG.warning("Entrée ignorée pour %r (objet attendu)", market_id)
            continue
        mid = market_id.strip()
        _validate_market_spec(spec, mid)
        out[mid] = spec
    return out




def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="Signaux METAR temps réel → realtime_signals.json")
    p.add_argument("--dry-run", action="store_true", help="Log seulement, n’écrit pas les fichiers")
    args = p.parse_args()

    try:
        mapping_path = icao_mapping_path()
        mapping = _load_icao_mapping(mapping_path)
    except Exception as e:
        LOG.exception("Lecture icao_mapping: %s", e)
        _write_meta_failure(
            "error_mapping",
            str(e),
            signals_written=0,
            targets_expected=None,
            pipeline_exit_code=1,
        )
        return 1

    if not mapping:
        LOG.info("Aucun marché dans icao_mapping — signaux vides.")
        payload = _empty_signals_payload()
        meta = _meta_ok(
            "Mapping vide",
            signals_written=0,
            targets_expected=0,
            extra={"source": "metar"},
        )
        if not args.dry_run:
            _write_json_atomic(realtime_signals_path(), payload)
            _write_json_atomic(realtime_signals_meta_path(), meta)
        return 0

    icaos = []
    for spec in mapping.values():
        icao = str(spec.get("icao", "")).strip().upper()
        if icao:
            icaos.append(icao)

    registry = get_metar_registry("bot1")
    try:
        obs_by_icao = registry.fetch_latest_for_stations(icaos)
    except Exception as e:
        LOG.exception("Échec API METAR: %s", e)
        _write_meta_failure(
            "error_metar_http",
            str(e),
            signals_written=0,
            targets_expected=len(mapping),
            pipeline_exit_code=1,
        )
        return 1

    default_deadline = os.getenv("BOT1_RT_DEADLINE_LOCAL_HHMM", "23:50")
    cycle_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    signals: list[dict[str, Any]] = []
    errors: dict[str, str] = {}

    for market_id, spec in mapping.items():
        icao = str(spec.get("icao", "")).strip().upper()
        if not icao:
            errors[market_id] = "icao manquant"
            continue
        thr_raw = spec.get("threshold")
        try:
            threshold = float(thr_raw)
        except (TypeError, ValueError):
            errors[market_id] = "threshold invalide"
            continue

        unit = str(spec.get("threshold_unit", "F") or "F")
        yes_if_above = spec.get("yes_if_above")
        if yes_if_above is None:
            above = True
        else:
            above = bool(yes_if_above)

        obs = obs_by_icao.get(icao)
        if obs is None:
            errors[market_id] = f"pas de METAR avec temp pour {icao}"
            continue

        tz_early = str(spec.get("timezone") or "UTC").strip() or "UTC"
        weather_var = str(spec.get("weather_variable") or "temp_max").strip().lower()
        day_state = None
        if weather_var in ("temp_max", "temp_min"):
            fetch_ds = getattr(registry, "fetch_day_state", None)
            if fetch_ds is not None:
                try:
                    day_state = fetch_ds(icao, _local_now(tz_early).date())
                except Exception as e:  # noqa: BLE001
                    LOG.warning("day state unavailable for %s: %s", icao, e)
        extreme_c = None
        if day_state is not None and int(getattr(day_state, "obs_count", 0) or 0) > 0:
            extreme_c = day_state.min_c if weather_var == "temp_min" else day_state.max_c
        source_c = obs.temp_celsius if extreme_c is None else float(extreme_c)
        observation_model = (
            "instant_metar" if extreme_c is None else ("daily_min" if weather_var == "temp_min" else "daily_max")
        )

        try:
            temp_contract = temp_celsius_to_contract_unit(source_c, unit)
        except ValueError as e:
            errors[market_id] = str(e)
            continue

        raw_delta = temp_contract - threshold
        tz = str(spec.get("timezone") or "UTC").strip() or "UTC"
        deadline = str(spec.get("deadline_local_hhmm") or default_deadline).strip()

        bucket = Bucket.from_mapping(spec)
        if extreme_c is not None and bucket is not None:
            # monotonic daily extreme: terminal-aware bucket decision on the whole-degree resolution value
            res_val = resolution_value(extreme_c, unit)
            d = decide_bucket(
                variable=weather_var,
                bucket=bucket,
                extreme_value=float(res_val),
                local_now=_local_now(tz),
                deadline_hhmm=deadline,
                dead_min_gap=float(spec.get("dead_min_gap", 15) or 15),
            )
            p_win = d.p_win
            binary_state = {"met": "attack", "dead": "flee", "open": "standby"}[d.binary_state]
            gap_unmet = abs(threshold - temp_contract) if binary_state != "attack" else 0.0
        else:
            p_win, binary_state, gap_unmet = _sniper_p_win(
                temp_contract=temp_contract,
                threshold=threshold,
                above=above,
                tz_name=tz,
                deadline_hhmm=deadline,
            )
        # Scalping YES uniquement si p_win=1.0 ; 0.5 = neutre (pas d’achat), 0.0 = mort
        recommended_side = "YES" if p_win >= 1.0 - 1e-9 else "NO"
        fc = obs.report_time_utc.strip() or cycle_stamp
        if not fc.endswith("Z") and "T" not in fc:
            fc = f"metar_{icao}_{fc}"

        obs_local = utc_observation_to_local(obs.observation_utc, tz)
        signals.append(
            {
                "market_id": market_id,
                "station": icao,
                "p_win": float(p_win),
                "delta": round(raw_delta, 2),
                "recommended_side": recommended_side,
                "forecast_cycle_utc": fc,
                "binary_state": binary_state,
                "gap_to_threshold": round(gap_unmet, 2) if binary_state != "attack" else 0.0,
                "metar_is_speci": obs.is_speci,
                "observation_model": observation_model,
                "instant_temp_c": obs.temp_celsius,
                "day_extreme_c": extreme_c,
                "day_obs_count": day_state.obs_count if day_state is not None else None,
                "observation_utc": obs.observation_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "observation_local": obs_local.isoformat(timespec="seconds"),
            }
        )
        LOG.info(
            "market=%s icao=%s temp=%.1f°%s thr=%.1f delta=%+.2f p_win=%s state=%s side=%s obs=%s",
            market_id,
            icao,
            temp_contract,
            unit[0],
            threshold,
            raw_delta,
            p_win,
            binary_state,
            recommended_side,
            obs.report_time_utc,
        )

    gen_unix = int(datetime.now(timezone.utc).timestamp())
    ttl_sec = _signals_ttl_sec()
    payload = {
        "last_run_utc": cycle_stamp,
        "last_run_unix": gen_unix,
        "generated_at_unix": gen_unix,
        "signals_ttl_sec": ttl_sec,
        "forecast_cycle_utc": f"metar_batch_{cycle_stamp}",
        "source": "metar",
        "signals": signals,
    }

    targets = len(mapping)
    if targets > 0 and not signals:
        reason = "Aucun signal METAR (stations ou API sans donnée exploitable)"
        LOG.error("%s — erreurs: %s", reason, errors)
        meta = _meta_failure(
            "error_no_signals",
            reason,
            signals_written=0,
            targets_expected=targets,
            pipeline_exit_code=1,
            generated_at_unix=gen_unix,
            signals_ttl_sec=ttl_sec,
            extra={"source": "metar", "observation_source": selected_source(), "metar_errors": errors},
        )
        if not args.dry_run:
            _write_json_atomic(realtime_signals_path(), payload)
            _write_json_atomic(realtime_signals_meta_path(), meta)
        return 1

    meta = _meta_ok(
        "METAR OK — sniper {attack: p_win=1, flee: p_win=0, standby: p_win=0.5}",
        signals_written=len(signals),
        targets_expected=targets,
        generated_at_unix=gen_unix,
        signals_ttl_sec=ttl_sec,
        extra={
            "source": "metar",
            "bot1_rt_science": "sniper_binary_v3",
            "metar_errors": errors if errors else None,
        },
    )
    if errors:
        LOG.warning("Signaux partiels — erreurs par marché: %s", errors)

    if not args.dry_run:
        _write_json_atomic(realtime_signals_path(), payload)
        _write_json_atomic(realtime_signals_meta_path(), meta)
    else:
        LOG.info("dry-run: %s signaux (non écrits)", len(signals))

    return 0


def _empty_signals_payload() -> dict[str, Any]:
    ts = datetime.now(timezone.utc)
    u = int(ts.timestamp())
    tss = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    ttl = _signals_ttl_sec()
    return {
        "last_run_utc": tss,
        "last_run_unix": u,
        "generated_at_unix": u,
        "signals_ttl_sec": ttl,
        "forecast_cycle_utc": "",
        "source": "metar",
        "signals": [],
    }


def _meta_ok(
    reason: str,
    *,
    signals_written: int,
    targets_expected: int,
    generated_at_unix: int | None = None,
    signals_ttl_sec: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ts_unix = int(datetime.now(timezone.utc).timestamp()) if generated_at_unix is None else generated_at_unix
    ts_utc = datetime.fromtimestamp(ts_unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ttl = _signals_ttl_sec() if signals_ttl_sec is None else signals_ttl_sec
    payload: dict[str, Any] = {
        "generated_at_utc": ts_utc,
        "generated_at_unix": ts_unix,
        "signals_ttl_sec": ttl,
        "forecast_cycle_utc": None,
        "status": "ok",
        "reason": reason,
        "signals_written": signals_written,
        "targets_expected": targets_expected,
        "wgrib2_available": None,
        "pipeline_exit_code": 0,
    }
    if extra:
        payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _meta_failure(
    status: str,
    reason: str,
    *,
    signals_written: int,
    targets_expected: int | None,
    pipeline_exit_code: int,
    generated_at_unix: int | None = None,
    signals_ttl_sec: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ts_unix = int(datetime.now(timezone.utc).timestamp()) if generated_at_unix is None else generated_at_unix
    ts_utc = datetime.fromtimestamp(ts_unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ttl = _signals_ttl_sec() if signals_ttl_sec is None else signals_ttl_sec
    payload: dict[str, Any] = {
        "generated_at_utc": ts_utc,
        "generated_at_unix": ts_unix,
        "signals_ttl_sec": ttl,
        "forecast_cycle_utc": None,
        "status": status,
        "reason": reason,
        "signals_written": signals_written,
        "targets_expected": targets_expected,
        "wgrib2_available": None,
        "pipeline_exit_code": pipeline_exit_code,
    }
    if extra:
        payload.update(extra)
    return payload


def _write_meta_failure(
    status: str,
    reason: str,
    *,
    signals_written: int,
    targets_expected: int | None,
    pipeline_exit_code: int,
) -> None:
    meta = _meta_failure(
        status,
        reason,
        signals_written=signals_written,
        targets_expected=targets_expected,
        pipeline_exit_code=pipeline_exit_code,
        extra={"source": "metar"},
    )
    _write_json_atomic(realtime_signals_meta_path(), meta)


if __name__ == "__main__":
    raise SystemExit(main())
