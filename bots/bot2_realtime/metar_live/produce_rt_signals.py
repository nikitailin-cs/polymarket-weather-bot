#!/usr/bin/env python3
"""
Bot 2 — Générateur de signaux temps réel : carte ``icao_mapping.json`` × METAR NOAA.

Science binaire : ``p_win`` 1.0 / 0.0 / 0.5, fuseaux via **pytz** (aligné demande projet).

Chaque marché doit avoir un ``target_date`` (YYYY-MM-DD) : sans ce champ, le marché est **ignoré**
(erreur log, pas de signal) — pas de fallback « legacy ».

**Portée** : soit ``target_date`` (jour unique), soit **plage** ``target_date_start`` + ``target_date_end``
(inclus, YYYY-MM-DD) — voir :mod:`core.rt_day_guard`.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytz

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.bot2_data_paths import REALTIME_SIGNALS_MAX_AGE_SEC, get_rt_paths  # noqa: E402
from core.rt_day_guard import classify_rt_market_day  # noqa: E402
from bots.bot2_realtime.metar_live.realtime_metar_registry import RealTimeMetarRegistry  # noqa: E402

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
    """
    Vérifie si le marché est 'mort' (impossible à gagner).
    Ex: Il est 23h30, on veut 80°F, il fait 60°F. C'est mort.
    """
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

    registry = RealTimeMetarRegistry(timeout=5.0)

    signals: dict[str, Any] = {}
    success_count = 0

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

        target_date_raw = str(config.get("target_date") or "").strip()
        target_date_start = str(config.get("target_date_start") or "").strip()
        target_date_end = str(config.get("target_date_end") or "").strip()
        has_range = bool(target_date_start and target_date_end)
        if not has_range and not target_date_raw:
            logger.error(
                "Missing target_date (ou target_date_start+end) pour marché RT %s — SKIP.",
                market_id[:18] + "…" if len(market_id) > 20 else market_id,
            )
            continue

        metar_data = registry.fetch_latest_metar(icao)
        if not metar_data:
            logger.warning("Ignoré: %s (Pas de donnée METAR pour %s)", market_id, icao)
            continue

        if unit in ("F", "FAHRENHEIT"):
            current_temp = float(metar_data["temp_f"])
        else:
            current_temp = float(metar_data["temp_c"])

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

        p_win = 0.5
        binary_state = "open"
        rec_side = "NONE"
        rt_day_guard: str

        if day_kind == "invalid":
            logger.error("target_date invalide pour %s : %r", market_id, target_date_raw)
            binary_state = "invalid_target"
            rt_day_guard = "invalid"

        elif day_kind == "premature":
            binary_state = "premature"
            rt_day_guard = "premature"
            logger.info(
                "Signal %s (%s) | garde jour J: prématuré (local=%s, target=%s) — pas de scalp RT",
                market_id[:20] + "…" if len(market_id) > 22 else market_id,
                icao,
                local_d.isoformat(),
                target_date_raw or "?",
            )

        elif day_kind == "past_target":
            binary_state = "past_target"
            rt_day_guard = "past_target"
            logger.info(
                "Signal %s (%s) | garde jour J: date locale après target (%s) — pas de scalp RT",
                market_id[:20] + "…" if len(market_id) > 22 else market_id,
                icao,
                target_date_raw or "?",
            )

        else:
            # jour_j : même jour calendaire que le contrat (timezone lieu)
            rt_day_guard = "jour_j"
            if is_above and current_temp >= threshold:
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

        sig_row: dict[str, Any] = {
            "p_win": p_win,
            "recommended_side": rec_side,
            "binary_state": binary_state,
            "current_obs_temp": current_temp,
            "raw_metar": metar_data.get("rawOb"),
            "obs_time_local": current_local_time.strftime("%Y-%m-%d %H:%M:%S"),
            "target_date": target_date_raw,
            "local_date": local_d.isoformat(),
            "rt_day_guard": rt_day_guard,
        }
        if has_range:
            sig_row["target_date_start"] = target_date_start
            sig_row["target_date_end"] = target_date_end
        signals[market_id] = sig_row
        success_count += 1

        logger.info(
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
    meta_data = {
        "timestamp": int(time.time()),
        "generated_at_unix": int(time.time()),
        "date_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "generated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "ok" if success_count > 0 else "error",
        "markets_processed": success_count,
        "bot2_science": "binary_p_win",
        "signals_ttl_sec": REALTIME_SIGNALS_MAX_AGE_SEC,
    }
    _write_json_atomic(meta_file, meta_data)

    logger.info("Cycle terminé. Manifeste écrit (Status: %s).", meta_data["status"])


if __name__ == "__main__":
    run_realtime_signals()
