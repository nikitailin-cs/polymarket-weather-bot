#!/usr/bin/env python3
"""
Bot 2 — Générateur de signaux temps réel : carte ``icao_mapping.json`` × METAR NOAA.

Science binaire : ``p_win`` 1.0 / 0.0 / 0.5, fuseaux via **pytz**.

**Portée date** : ``target_date`` seul, ou plage ``target_date_start`` + ``target_date_end``
(inclus). Politique optionnelle ``range_scalp_mode`` (:mod:`core.rt_range_policy`).

**Variables** : ``weather_variable`` parmi ``temp_max`` / ``temp_min`` / ``temp_avg`` (défaut
``temp_max``) ; autre valeur → ``binary_state=unsupported_variable`` (pas de METAR).

**Alignement** : ``metar_interpretation`` / ``RT_METAR_INTERPRETATION`` — voir
:mod:`core.rt_polymarket_alignment`.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytz

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.bot2_data_paths import REALTIME_SIGNALS_MAX_AGE_SEC, get_rt_paths  # noqa: E402
from core.rt_day_guard import classify_rt_market_day  # noqa: E402
from core.rt_polymarket_alignment import (  # noqa: E402
    SIGNAL_RESOLUTION_MODEL_INSTANT_METAR,
    MetarInterpretation,
    is_supported_weather_variable,
    resolve_metar_interpretation,
)
from core.rt_range_policy import narrow_range_scalp_day  # noqa: E402
from core.rt_signal_health import summarize_signals_health  # noqa: E402
from monitoring.error_guard import run_guarded  # noqa: E402
from core.metar_source import get_metar_registry  # noqa: E402
from core.rt_daily_extrema import RESOLUTION_MODEL_DAILY_EXTREMA, Bucket  # noqa: E402
from strategies import MarketContext, load_strategies  # noqa: E402
from core.station_day_state import resolution_value  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("produce_rt_signals")


def check_if_dead(
    current_local_time: datetime,
    deadline_str: str,
    current_temp: float,
    threshold: float,
    gap: float,
) -> bool:
    try:
        dead_h, dead_m = map(int, deadline_str.split(":"))
        minutes_to_deadline = (dead_h - current_local_time.hour) * 60 + (
            dead_m - current_local_time.minute
        )

        if minutes_to_deadline < 120 and abs(threshold - current_temp) > gap:
            return True
        if minutes_to_deadline <= 0:
            return True

        return False
    except Exception as e:
        logger.error("Erreur calcul deadline : %s", e)
        return False


def _fetch_day_state(registry: Any, icao: str, local_d: Any, weather_var: str) -> Any:
    """Running daily extremes from the METAR.ws snapshot registry (None for AWC polling)."""
    if weather_var not in ("temp_max", "temp_min"):
        return None
    fetch = getattr(registry, "fetch_day_state", None)
    if fetch is None:
        return None
    try:
        return fetch(icao, local_d)
    except Exception as e:  # noqa: BLE001
        logger.warning("day state unavailable for %s: %s", icao, e)
        return None


def _day_extreme(state: Any, weather_var: str, unit: str) -> Optional[dict[str, Any]]:
    """Today's max (or min) in the contract unit, with provenance, or None."""
    if state is None or int(getattr(state, "obs_count", 0) or 0) <= 0:
        return None
    if weather_var == "temp_min":
        value_c, t, src, raw = state.min_c, state.min_time_utc, state.min_source, state.min_raw
        lead_c, lead_speci = getattr(state, "lead_min_c", None), bool(getattr(state, "lead_min_is_speci", False))
    else:
        value_c, t, src, raw = state.max_c, state.max_time_utc, state.max_source, state.max_raw
        lead_c, lead_speci = getattr(state, "lead_max_c", None), bool(getattr(state, "lead_max_is_speci", False))
    if value_c is None:
        return None
    if unit in ("F", "FAHRENHEIT"):
        value = round(float(value_c) * 9.0 / 5.0 + 32.0, 1)
    else:
        value = round(float(value_c), 1)
    # whole-degree value per the market rule (routine reports, tenths, halves rounded up)
    res_value = resolution_value(float(value_c), unit)
    coverage_ok, first_obs = _day_coverage(state)
    return {
        "kind": weather_var,
        "value": value,
        "resolution_value": res_value,
        "value_c": float(value_c),
        "coverage_ok": coverage_ok,
        "first_obs_utc": first_obs,
        "lead_value_c": lead_c,
        "lead_resolution_value": resolution_value(lead_c, unit) if lead_c is not None else None,
        "lead_is_speci": lead_speci,
        "time_utc": t,
        "source": src,
        "is_speci": False,
        "raw": raw,
        "local_date": state.local_date,
        "timezone": state.timezone,
        "obs_count": state.obs_count,
        "speci_count": state.speci_count,
        "last_temp_c": state.last_temp_c,
    }


STRATEGIES = load_strategies()


def _fetch_forecasts(registry: Any, icao: str, local_d: Any) -> dict[str, Any]:
    fetch = getattr(registry, "fetch_forecasts", None)
    if fetch is None:
        return {}
    try:
        return dict(fetch(icao, local_d) or {})
    except Exception as e:  # noqa: BLE001
        logger.warning("forecasts unavailable for %s: %s", icao, e)
        return {}


def _day_end_utc(tz_name: str, local_d: Any) -> Optional[datetime]:
    try:
        tz = pytz.timezone(str(tz_name or "UTC"))
        d = date.fromisoformat(str(local_d))
        return tz.localize(datetime.combine(d + timedelta(days=1), datetime.min.time())).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001 - unknown zone/date: no late rule
        return None


def _market_context(
    market_id: str, icao: str, weather_var: str, unit: str, bucket: Any, local_d: Any, tz_name: str,
    local_now: datetime, deadline_str: str, dead_gap: float, extreme: dict[str, Any], forecasts: dict[str, Any],
) -> MarketContext:
    """Facts for the strategies: bucket, observed routine extreme, coverage, forecasts, time left."""
    return MarketContext(
        market_id=market_id,
        icao=icao,
        variable=weather_var,
        unit=unit,
        bucket=bucket,
        target_date=str(local_d),
        timezone=tz_name,
        local_now=local_now,
        deadline_hhmm=deadline_str,
        dead_min_gap=dead_gap,
        day_end_utc=_day_end_utc(tz_name, local_d),
        extreme_c=extreme.get("value_c"),
        resolution_value=extreme.get("resolution_value"),
        coverage_ok=bool(extreme.get("coverage_ok", False)),
        obs_count=int(extreme.get("obs_count") or 0),
        lead_value_c=extreme.get("lead_value_c"),
        forecasts=forecasts,
    )


def _run_strategies(ctx: MarketContext) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Every strategy's signal fields merged, plus the intents of the enabled ones."""
    fields: dict[str, Any] = {}
    intents: list[dict[str, Any]] = []
    for strat in STRATEGIES:
        try:
            a = strat.evaluate(ctx)
        except Exception as e:  # noqa: BLE001 - one broken strategy must not stop the cycle
            logger.exception("strategy %s failed on %s: %s", strat.name, ctx.market_id[:16], e)
            continue
        fields.update(a.fields)
        if a.intent is not None:
            intents.append(a.intent.to_dict())
    return fields, intents


def _day_coverage(state: Any, grace_min: int = 90) -> tuple[bool, str]:
    """
    True when the day state's first routine report falls within ``grace_min`` of the station's
    local midnight, i.e. the running extreme covers the whole day so far. A daemon started
    mid-day gives a partial extreme: still a bound for the probability, never "the day's value".
    """
    first = str(getattr(state, "first_obs_utc", "") or "")
    if not first:
        return False, ""
    try:
        tz = pytz.timezone(str(getattr(state, "timezone", "") or "UTC"))
        d = date.fromisoformat(str(state.local_date))
        day_start = tz.localize(datetime.combine(d, datetime.min.time())).astimezone(timezone.utc)
        first_dt = datetime.fromisoformat(first.replace("Z", "+00:00"))
        return (first_dt - day_start).total_seconds() <= grace_min * 60, first
    except Exception:  # noqa: BLE001
        return False, first


def _observation_note(weather_var: str, *, use_extrema: bool = False) -> str:
    v = (weather_var or "temp_max").strip().lower()
    if use_extrema:
        return (
            "Daily extreme built from the METAR.ws push stream (every METAR and SPECI, station local day). "
            "Still verify the market official resolution source and rounding."
        )
    if v == "temp_max":
        return (
            "METAR = température instantanée ; si le marché vise un max journalier officiel, "
            "l’oracle peut diverger."
        )
    if v in ("temp_min", "temp_avg"):
        return (
            "Le flux utilise la même température METAR instantanée pour toutes les variables supportées ; "
            "min/moyenne journalière officielle peut différer."
        )
    return ""


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8")
    tmp.replace(path)


def run_realtime_signals() -> None:
    paths = get_rt_paths()
    mapping_file = paths["mapping"]
    signals_file = paths["signals"]
    meta_file = paths["meta"]

    if not mapping_file.is_file():
        logger.error("Fichier de mapping introuvable: %s", mapping_file)
        return

    with open(mapping_file, encoding="utf-8") as f:
        markets: dict[str, Any] = json.load(f)

    logger.info("Démarrage analyse RT sur %s entrée(s) mapping.", len(markets))

    registry = get_metar_registry("bot2")

    signals: dict[str, Any] = {}
    processed = 0

    for market_id, config in markets.items():
        if not isinstance(market_id, str) or not market_id.strip():
            continue
        if market_id.startswith("_"):
            continue
        if not isinstance(config, dict):
            logger.warning("Ignoré %r (config non-objet)", market_id)
            continue

        try:
            icao = str(config["icao"]).strip().upper()
            threshold = float(config["threshold"])
            is_above = bool(config.get("yes_if_above", True))
            unit = str(config.get("threshold_unit", "F") or "F").upper()
            tz_name = str(config.get("timezone") or "UTC").strip() or "UTC"
            deadline_str = str(config.get("deadline_local_hhmm") or "23:59").strip()
            try:
                dead_gap = float(config.get("dead_min_gap", 15))
            except (TypeError, ValueError):
                dead_gap = 15.0
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Config invalide pour %s : %s", market_id, e)
            continue

        res_notes = str(config.get("resolution_notes") or "").strip()
        if res_notes:
            logger.warning(
                "Marché %s : resolution_notes présent — vérifier le contrat Polymarket / UMA : %s",
                market_id[:24] + "…" if len(market_id) > 26 else market_id,
                res_notes[:200],
            )

        target_date_raw = str(config.get("target_date") or "").strip()
        target_date_start = str(config.get("target_date_start") or "").strip()
        target_date_end = str(config.get("target_date_end") or "").strip()
        has_range = bool(target_date_start and target_date_end)
        range_scalp_mode = str(config.get("range_scalp_mode") or "").strip()

        if not has_range and not target_date_raw:
            logger.error(
                "Missing target_date (ou target_date_start+end) pour marché RT %s — SKIP.",
                market_id[:18] + "…" if len(market_id) > 20 else market_id,
            )
            continue

        weather_var = str(config.get("weather_variable", "temp_max") or "temp_max").strip().lower()
        alignment = resolve_metar_interpretation(config)

        try:
            local_tz = pytz.timezone(tz_name)
        except Exception:
            logger.warning("Timezone invalide %r pour %s — UTC", tz_name, market_id)
            local_tz = pytz.UTC
        current_local_time = datetime.now(local_tz)
        local_d = current_local_time.date()

        day_kind = classify_rt_market_day(
            local_d,
            target_date_raw,
            target_date_start=target_date_start,
            target_date_end=target_date_end,
        )

        range_policy_reason: Optional[str] = None
        if day_kind == "jour_j" and has_range:
            nd, sub = narrow_range_scalp_day(
                local_d,
                has_range=has_range,
                target_date_start=target_date_start,
                target_date_end=target_date_end,
                range_scalp_mode=range_scalp_mode,
            )
            if nd == "premature" and sub:
                day_kind = "premature"
                range_policy_reason = sub

        if not is_supported_weather_variable(weather_var):
            logger.warning(
                "Variable météo non supportée %r pour %s — unsupported_variable",
                weather_var,
                market_id,
            )
            sig_row: dict[str, Any] = {
                "p_win": 0.5,
                "recommended_side": "NONE",
                "binary_state": "unsupported_variable",
                "current_obs_temp": None,
                "raw_metar": None,
                "obs_time_local": current_local_time.strftime("%Y-%m-%d %H:%M:%S"),
                "target_date": target_date_raw,
                "local_date": local_d.isoformat(),
                "rt_day_guard": day_kind if day_kind != "jour_j" else "jour_j",
                "weather_variable": weather_var,
                "metar_interpretation": alignment.interpretation.value,
                "alignment_disclaimer": alignment.disclaimer,
                "resolution_model": SIGNAL_RESOLUTION_MODEL_INSTANT_METAR,
                "observation_note": _observation_note(weather_var),
            }
            if has_range:
                sig_row["target_date_start"] = target_date_start
                sig_row["target_date_end"] = target_date_end
            if range_policy_reason:
                sig_row["range_policy_reason"] = range_policy_reason
            signals[market_id] = sig_row
            processed += 1
            continue

        if alignment.interpretation == MetarInterpretation.DOCUMENT_ONLY:
            sig_row = {
                "p_win": 0.5,
                "recommended_side": "NONE",
                "binary_state": "document_only",
                "current_obs_temp": None,
                "raw_metar": None,
                "obs_time_local": current_local_time.strftime("%Y-%m-%d %H:%M:%S"),
                "target_date": target_date_raw,
                "local_date": local_d.isoformat(),
                "rt_day_guard": day_kind if day_kind != "jour_j" else "jour_j",
                "weather_variable": weather_var,
                "metar_interpretation": alignment.interpretation.value,
                "alignment_disclaimer": alignment.disclaimer,
                "resolution_model": SIGNAL_RESOLUTION_MODEL_INSTANT_METAR,
                "observation_note": _observation_note(weather_var),
            }
            if has_range:
                sig_row["target_date_start"] = target_date_start
                sig_row["target_date_end"] = target_date_end
            if range_policy_reason:
                sig_row["range_policy_reason"] = range_policy_reason
            signals[market_id] = sig_row
            processed += 1
            continue

        metar_data = registry.fetch_latest_metar(icao)
        day_state = _fetch_day_state(registry, icao, local_d, weather_var)
        extreme = _day_extreme(day_state, weather_var, unit)
        if not metar_data and extreme is None:
            logger.warning("Pas de METAR pour %s (%s) — metar_unavailable", market_id, icao)
            sig_row = {
                "p_win": 0.5,
                "recommended_side": "NONE",
                "binary_state": "metar_unavailable",
                "current_obs_temp": None,
                "raw_metar": None,
                "obs_time_local": current_local_time.strftime("%Y-%m-%d %H:%M:%S"),
                "target_date": target_date_raw,
                "local_date": local_d.isoformat(),
                "rt_day_guard": day_kind if day_kind != "jour_j" else "jour_j",
                "weather_variable": weather_var,
                "metar_interpretation": alignment.interpretation.value,
                "alignment_disclaimer": alignment.disclaimer,
                "resolution_model": SIGNAL_RESOLUTION_MODEL_INSTANT_METAR,
                "observation_note": _observation_note(weather_var),
            }
            if has_range:
                sig_row["target_date_start"] = target_date_start
                sig_row["target_date_end"] = target_date_end
            if range_policy_reason:
                sig_row["range_policy_reason"] = range_policy_reason
            signals[market_id] = sig_row
            processed += 1
            continue

        instant_temp: Optional[float] = None
        if metar_data:
            if unit in ("F", "FAHRENHEIT"):
                instant_temp = float(metar_data["temp_f"])
            else:
                instant_temp = float(metar_data["temp_c"])
        # Daily-extreme markets decide on the running max/min of the station's local day
        # (METAR.ws push), falling back to the instantaneous METAR when no day state exists.
        # decisions use the whole-degree resolution value; the precise extreme stays in the row
        current_temp = float(extreme["resolution_value"]) if extreme is not None else float(instant_temp)
        bucket = Bucket.from_mapping(config)
        strategy_fields: dict[str, Any] = {}
        intents: list[dict[str, Any]] = []
        if extreme is not None and bucket is not None and weather_var in ("temp_max", "temp_min"):
            ctx = _market_context(
                market_id, icao, weather_var, unit, bucket, local_d, tz_name, current_local_time, deadline_str, dead_gap,
                extreme, _fetch_forecasts(registry, icao, local_d),
            )
            strategy_fields, intents = _run_strategies(ctx)
        model_prob = strategy_fields.get("forecast")
        resolution_model = (
            RESOLUTION_MODEL_DAILY_EXTREMA if extreme is not None else SIGNAL_RESOLUTION_MODEL_INSTANT_METAR
        )

        p_win = 0.5
        binary_state = "open"
        rec_side = "NONE"
        decision_reason = ""
        rt_day_guard: str

        if day_kind == "invalid":
            logger.error("target_date invalide pour %s : %r", market_id, target_date_raw)
            binary_state = "invalid_target"
            rt_day_guard = "invalid"

        elif day_kind == "premature":
            binary_state = "premature"
            rt_day_guard = "premature"
            logger.debug(
                "Signal %s (%s) | garde jour J: prématuré (local=%s) — pas de scalp RT",
                market_id[:20] + "…" if len(market_id) > 22 else market_id,
                icao,
                local_d.isoformat(),
            )

        elif day_kind == "past_target":
            binary_state = "past_target"
            rt_day_guard = "past_target"
            logger.debug(
                "Signal %s (%s) | garde jour J: après fenêtre contrat — pas de scalp RT",
                market_id[:20] + "…" if len(market_id) > 22 else market_id,
                icao,
            )

        else:
            rt_day_guard = "jour_j"
            if extreme is not None and bucket is not None and "binary_state" in strategy_fields:
                p_win = float(strategy_fields.get("p_win", 0.5))
                binary_state = str(strategy_fields.get("binary_state") or "open")
                decision_reason = str(strategy_fields.get("decision_reason") or "")
            elif is_above and current_temp >= threshold:
                p_win = 1.0
                binary_state = "met"
            elif not is_above and current_temp < threshold:
                p_win = 1.0
                binary_state = "met"
            elif check_if_dead(
                current_local_time,
                deadline_str,
                current_temp,
                threshold,
                dead_gap,
            ):
                p_win = 0.0
                binary_state = "dead"

            if p_win > 0.5:
                rec_side = "YES"
            elif p_win < 0.5:
                rec_side = "NO"
            else:
                rec_side = "NONE"

        sig_row = {
            "p_win": p_win,
            "recommended_side": rec_side,
            "binary_state": binary_state,
            "current_obs_temp": current_temp,
            "instant_obs_temp": instant_temp,
            "day_extreme": extreme,
            "bucket": {"low": bucket.low, "high": bucket.high} if bucket is not None else None,
            "decision_reason": decision_reason,
            "p_model": model_prob["p"] if model_prob else None,
            "forecast": model_prob,
            "intents": intents if rt_day_guard == "jour_j" else [],
            "strategy_notes": {k: v for k, v in strategy_fields.items() if k.endswith("_skip")},
            "raw_metar": metar_data.get("rawOb") if metar_data else (extreme or {}).get("raw"),
            "obs_time_local": current_local_time.strftime("%Y-%m-%d %H:%M:%S"),
            "target_date": target_date_raw,
            "local_date": local_d.isoformat(),
            "rt_day_guard": rt_day_guard,
            "weather_variable": weather_var,
            "metar_interpretation": alignment.interpretation.value,
            "alignment_disclaimer": alignment.disclaimer,
            "resolution_model": resolution_model,
            "observation_note": _observation_note(weather_var, use_extrema=extreme is not None),
        }
        if has_range:
            sig_row["target_date_start"] = target_date_start
            sig_row["target_date_end"] = target_date_end
        if range_policy_reason:
            sig_row["range_policy_reason"] = range_policy_reason

        signals[market_id] = sig_row
        processed += 1

        (logger.info if rt_day_guard == "jour_j" else logger.debug)(
            "Signal %s (%s) | Temp: %s | Seuil: %s | P_WIN: %s | État: %s",
            market_id[:20] + "…" if len(market_id) > 22 else market_id,
            icao,
            current_temp,
            threshold,
            p_win,
            binary_state,
        )

    _write_json_atomic(signals_file, signals)

    now_utc = datetime.now(timezone.utc)
    health = summarize_signals_health(signals)
    meta_data = {
        "timestamp": int(time.time()),
        "generated_at_unix": int(time.time()),
        "date_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "generated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "ok" if processed > 0 else "error",
        "markets_processed": processed,
        "bot2_science": "binary_p_win",
        "signals_ttl_sec": REALTIME_SIGNALS_MAX_AGE_SEC,
        "signals_health": health,
    }
    _write_json_atomic(meta_file, meta_data)

    logger.info("Cycle terminé. Manifeste écrit (Status: %s, health=%s).", meta_data["status"], health.get("health_status"))


if __name__ == "__main__":
    raise SystemExit(run_guarded("bot2.produce_rt_signals", lambda: (run_realtime_signals() or 0)))
