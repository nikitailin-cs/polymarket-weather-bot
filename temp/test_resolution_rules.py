"""
Test visuel: extraction des "Resolution rules" depuis Gamma (Polymarket).

Usage:
  python temp/test_resolution_rules.py
  python temp/test_resolution_rules.py --limit 80 --show-all
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    # fallback: variables d'env système seulement
    pass

from scanners.market_scanner import _build_market, _extract_resolution_rules, _fetch_gamma  # noqa: E402


def _fmt(s: str, max_len: int = 300) -> str:
    s = " ".join((s or "").split())
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


_SOURCE_PATTERNS: list[tuple[str, str]] = [
    (r"\b(noaa|nws|national weather service|ghcnd)\b", "noaa_ghcnd"),
    (r"\b(wmo|world meteorological organization|ogimet)\b", "wmo_station"),
    (r"\b(nhc|national hurricane center)\b", "nhc"),
    (r"\b(meteo[- ]france)\b", "meteo_france"),
    (r"\b(copernicus)\b", "copernicus"),
    (r"\b(weather\.com)\b", "weather_com"),
    (r"\b(accuweather)\b", "accuweather"),
    (r"\b(wunderground|weather underground)\b", "wunderground"),
    (r"\b(hong kong observatory|weather\.gov\.hk)\b", "hk_observatory"),
    (r"\b(nasa giss|giss\.nasa\.gov|data\.giss\.nasa\.gov)\b", "nasa_giss"),
]


def _extract_urls(text: str) -> list[str]:
    if not text:
        return []
    return re.findall(r"https?://[^\s)\]>\"']+", text, flags=re.IGNORECASE)


def _extract_source_name(text: str) -> str:
    tl = (text or "").lower()
    for pattern, source in _SOURCE_PATTERNS:
        if re.search(pattern, tl):
            return source
    return "unknown"


def _fetch_gamma_custom(limit: int, active: bool, closed: bool) -> list[dict]:
    """
    Fetch Gamma avec flags paramétrables pour diversifier les templates de règles.
    """
    base = "https://gamma-api.polymarket.com/markets"
    params = {
        "active": str(active).lower(),
        "closed": str(closed).lower(),
        "limit": str(limit),
        "order": "volume",
        "ascending": "false",
    }
    url = f"{base}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _wrap_block(text: str, width: int = 120) -> str:
    """
    Wrap robuste pour affichage terminal (évite les lignes géantes coupées visuellement).
    """
    if not text:
        return ""
    parts = []
    for raw_line in text.splitlines() or [text]:
        line = raw_line.strip()
        if not line:
            parts.append("")
            continue
        parts.append("\n".join(textwrap.wrap(line, width=width, break_long_words=False)))
    return "\n".join(parts)


def _extract_station_from_rules(text: str) -> str:
    if not text:
        return ""
    patterns = [
        r"recorded at the\s+(.+?)\s+Station",
        r"for the\s+(.+?)\s+Station",
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            return " ".join(m.group(1).split()).strip()
    return ""


def _extract_date_from_rules(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"on\s+(\d{1,2}\s+[A-Za-z]{3}\s+'?\d{2,4})", text, flags=re.IGNORECASE)
    if not m:
        return ""
    return m.group(1).strip()


def _extract_variable_from_rules(text: str) -> str:
    tl = (text or "").lower()
    if "highest temperature" in tl:
        return "temp_max"
    if "lowest temperature" in tl:
        return "temp_min"
    if "temperature" in tl:
        return "temp"
    if "rain" in tl or "precip" in tl:
        return "precip"
    return "unknown"


def _is_weather_contract(question: str, rules_text: str) -> bool:
    q = (question or "").lower()
    r = (rules_text or "").lower()
    weather_markers = (
        "temperature", "global temperature", "highest temperature", "lowest temperature",
        "precipitation", "rainfall", "snowfall", "wind speed", "humidity",
    )
    return any(m in q for m in weather_markers) or any(m in r for m in weather_markers)


def _extract_unit_from_rules(text: str) -> str:
    tl = (text or "").lower()
    if "degrees celsius" in tl or "°c" in tl:
        return "celsius"
    if "degrees fahrenheit" in tl or "°f" in tl:
        return "fahrenheit"
    if "mm" in tl:
        return "mm"
    if "cm" in tl:
        return "cm"
    return "unknown"


def _math_rule_from_question(question: str) -> dict:
    """
    Conversion regex de la question -> règle mathématique.
    """
    q = question or ""
    ql = q.lower()
    city = ""
    m_city = re.search(r"in\s+([A-Za-z\s]+?)\s+be\s+", q, flags=re.IGNORECASE)
    if m_city:
        city = " ".join(m_city.group(1).split()).strip()
    elif "global temperature" in ql:
        city = "GLOBAL"

    value = None
    value_range = None
    unit = "unknown"

    m_between = re.search(
        r"between\s+([-+]?\d+(?:\.\d+)?)\s*[-–]\s*([-+]?\d+(?:\.\d+)?)\s*[°º]?\s*([CFcf])",
        q,
        flags=re.IGNORECASE,
    )
    if m_between:
        lo = float(m_between.group(1))
        hi = float(m_between.group(2))
        if lo > hi:
            lo, hi = hi, lo
        value_range = [lo, hi]
        value = hi
        unit = "celsius" if m_between.group(3).upper() == "C" else "fahrenheit"
    else:
        m_val_unit = re.search(r"\bbe\s+([-+]?\d+(?:\.\d+)?)\s*[°º]?\s*([CFcf])", q)
        if m_val_unit:
            value = float(m_val_unit.group(1))
            unit = "celsius" if m_val_unit.group(2).upper() == "C" else "fahrenheit"

    op = "eq"
    if value_range is not None:
        op = "between"
    elif any(x in ql for x in ("or higher", "or above", "at least", ">= ", "≥")):
        op = "gte"
    elif any(x in ql for x in ("or lower", "or below", "at most", "<= ", "≤")):
        op = "lte"
    elif any(x in ql for x in ("above", "greater than", "over", "exceed")):
        op = "gt"
    elif any(x in ql for x in ("below", "less than", "under")):
        op = "lt"

    variable = "unknown"
    if "highest temperature" in ql:
        variable = "temp_max"
    elif "lowest temperature" in ql:
        variable = "temp_min"
    elif "temperature" in ql:
        variable = "temp"

    m_date = re.search(
        r"\bon\b\s+([A-Za-z]+)\s+(\d{1,2})",
        q,
        flags=re.IGNORECASE,
    )
    if m_date:
        date_raw = f"{m_date.group(1)} {m_date.group(2)}"
    else:
        m_date2 = re.search(r"\bin\b\s+([A-Za-z]+)\s+(\d{4})", q, flags=re.IGNORECASE)
        date_raw = f"{m_date2.group(1)} {m_date2.group(2)}" if m_date2 else ""

    return {
        "city": city,
        "variable": variable,
        "op": op,
        "value": value,
        "value_range": value_range,
        "unit": unit,
        "date": date_raw,
    }


def _render_math_rule(rule: dict, station: str, source: str) -> str:
    val = "?" if rule.get("value") is None else str(rule.get("value"))
    if rule.get("op") == "between" and rule.get("value_range"):
        lo, hi = rule["value_range"]
        val = f"[{lo}, {hi}]"
    unit = rule.get("unit", "unknown")
    op_map = {"eq": "=", "gte": ">=", "lte": "<=", "gt": ">", "lt": "<", "between": "in"}
    op = op_map.get(rule.get("op", "eq"), "=")
    var = rule.get("variable", "unknown")
    date_raw = rule.get("date", "?")
    city = str(rule.get("city") or "").strip()
    if city.upper() == "GLOBAL":
        station_txt = "Global_Land_Ocean_Index"
    else:
        station_txt = station or city or "station_inconnue"
    source_txt = source or "source_inconnue"
    return (
        f"resolve_yes <=> {var}({station_txt}, {date_raw}) {op} {val} [{unit}] ; "
        f"source_officielle={source_txt}"
    )


def _llm_math_from_rules(question: str, rules_text: str) -> dict:
    """
    Fallback LLM (cheap) pour transformer rules+question en règle mathématique JSON.
    """
    from core.contract_parser import _llm_extract_source_from_rules_with_diag  # noqa: E402
    source, confidence, source_url, mode, reason = _llm_extract_source_from_rules_with_diag(
        rules_text=rules_text,
        gamma_resolution_source="",
    )
    base = _math_rule_from_question(question)
    base["source_name"] = source
    base["source_url"] = source_url
    base["confidence"] = round(confidence, 3)
    base["llm_mode"] = mode
    base["llm_reason"] = reason
    return base


def _load_input_json(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        return []
    rows: list[dict] = []
    for r in data:
        if not isinstance(r, dict):
            continue
        question = str(r.get("question") or "")
        condition_id = str(r.get("condition_id") or r.get("conditionId") or r.get("id") or "")
        resolution_source = str(r.get("resolution_source_field") or r.get("resolutionSource") or "")
        rules = str(r.get("rules_text") or r.get("resolutionRules") or r.get("description") or "")
        rows.append(
            {
                "_question": question,
                "_condition_id": condition_id,
                "_resolution_source": resolution_source,
                "_rules_text": rules,
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspecte visuellement les resolution rules.")
    parser.add_argument("--limit", type=int, default=50, help="Nombre de marches Gamma a lire")
    parser.add_argument(
        "--show-all",
        action="store_true",
        help="Afficher aussi les marches sans rules detectees",
    )
    parser.add_argument(
        "--full-rules",
        action="store_true",
        help="Afficher le texte complet des rules (sans troncature)",
    )
    parser.add_argument(
        "--dump-file",
        type=str,
        default="",
        help="Chemin de sortie (ex: temp/resolution_rules_dump.txt)",
    )
    parser.add_argument(
        "--with-llm",
        action="store_true",
        help="Active le fallback LLM (Anthropic) pour les cas ambigus/unknown",
    )
    parser.add_argument(
        "--llm-always",
        action="store_true",
        help="Force un appel LLM sur chaque marche (demo/diagnostic)",
    )
    parser.add_argument(
        "--show-math",
        action="store_true",
        help="Affiche la transformation Avant->Apres en regle mathematique",
    )
    parser.add_argument(
        "--include-closed",
        action="store_true",
        help="Inclut aussi les marchés fermés/résolus pour diversifier les sources",
    )
    parser.add_argument(
        "--exclude-source",
        type=str,
        default="",
        help="Exclut une source (ex: wunderground)",
    )
    parser.add_argument(
        "--only-source",
        type=str,
        default="",
        help="N'affiche qu'une source (ex: noaa_ghcnd, weather_com, unknown)",
    )
    parser.add_argument(
        "--input-json",
        type=str,
        default="",
        help="Dataset JSON local (ex: data/raw/weather_markets_bulk.json) au lieu de Gamma live",
    )
    args = parser.parse_args()

    input_rows: list[dict] = []
    raw_markets: list[dict] = []
    if args.input_json:
        input_rows = _load_input_json(args.input_json)
        print(f"Input JSON mode: {args.input_json}")
        print(f"Recu: {len(input_rows)} enregistrements\n")
    else:
        print(f"Gamma fetch... limit={args.limit}")
        if args.include_closed:
            raw_a = _fetch_gamma_custom(limit=args.limit, active=True, closed=False)
            raw_b = _fetch_gamma_custom(limit=args.limit, active=False, closed=True)
            uniq: dict[str, dict] = {}
            for raw in raw_a + raw_b:
                cid = str(raw.get("conditionId") or raw.get("id") or "")
                key = cid or str(raw.get("question") or "")
                if key and key not in uniq:
                    uniq[key] = raw
            raw_markets = list(uniq.values())
            print(f"Recu: {len(raw_markets)} marches (active + closed)\n")
        else:
            raw_markets = _fetch_gamma(limit=args.limit)
            print(f"Recu: {len(raw_markets)} marches\n")

    weather_total = 0
    weather_with_rules = 0
    weather_without_rules = 0
    displayed_total = 0
    llm_model_calls = 0
    llm_fallback_calls = 0

    dump_lines: list[str] = []

    iterable = input_rows if args.input_json else raw_markets
    for raw in iterable:
        if args.input_json:
            question = raw.get("_question", "")
            condition_id = raw.get("_condition_id", "")
            resolution_source = raw.get("_resolution_source", "")
            rules = raw.get("_rules_text", "")
            if not _is_weather_contract(question, rules):
                continue
        else:
            built = _build_market(raw)
            if built is None:
                continue
            market, resolution_source, _liquidity_usd = built
            question = market.question
            condition_id = market.condition_id
            rules = _extract_resolution_rules(raw)
            if not _is_weather_contract(question, rules):
                continue

        has_rules = bool(rules.strip())
        rules_urls = _extract_urls(rules)
        rules_source = _extract_source_name(rules)
        gamma_source = str(resolution_source or "")
        gamma_source_name = _extract_source_name(gamma_source)
        llm_source = "n/a"
        llm_conf = 0.0
        llm_url = ""
        final_source = rules_source if rules_source != "unknown" else gamma_source_name

        # Filtres de diversité de test
        ex = (args.exclude_source or "").strip().lower()
        only = (args.only_source or "").strip().lower()
        if ex and final_source == ex:
            continue
        if only and final_source != only:
            continue

        conflict = (
            rules_source != "unknown" and
            gamma_source_name != "unknown" and
            rules_source != gamma_source_name
        )
        llm_needed = (final_source == "unknown" or conflict or args.llm_always)
        if args.with_llm and llm_needed:
            from core.contract_parser import _llm_extract_source_from_rules_with_diag  # noqa: E402
            llm_source, llm_conf, llm_url, llm_mode, llm_reason = _llm_extract_source_from_rules_with_diag(
                rules_text=rules,
                gamma_resolution_source=gamma_source,
            )
            if llm_source != "unknown":
                final_source = llm_source
            if llm_mode in ("model", "cache"):
                llm_model_calls += 1
            elif llm_mode.startswith("fallback"):
                llm_fallback_calls += 1
        else:
            llm_mode = "skipped"
            llm_reason = "not_needed"

        if has_rules:
            weather_with_rules += 1
        else:
            weather_without_rules += 1
            if not args.show_all:
                continue
        weather_total += 1
        displayed_total += 1

        print("-" * 110)
        print(f"Q: {question}")
        print(f"condition_id: {condition_id}")
        print(f"gamma.resolutionSource: {resolution_source or 'N/A'}")
        print(f"rules_detectees: {'YES' if has_rules else 'NO'}")
        print(f"step2.source_from_rules: {rules_source}")
        print(f"step2.source_from_gamma_resolutionSource: {gamma_source_name}")
        if args.with_llm:
            if llm_needed:
                print(f"step2.source_from_llm: {llm_source} (conf={llm_conf:.2f})")
                print(f"step2.llm_mode: {llm_mode}")
                print(f"step2.llm_reason: {llm_reason}")
            else:
                print("step2.source_from_llm: skipped (regex/gamma non ambigu)")
            if llm_url:
                print(f"step2.url_from_llm: {llm_url}")
        print(f"step2.final_source: {final_source}")
        if rules_urls:
            print(f"step2.urls_from_rules: {len(rules_urls)}")
            print(f"step2.first_url_from_rules: {rules_urls[0]}")
        else:
            print("step2.urls_from_rules: 0")
        if has_rules:
            rules_text = rules if args.full_rules else _fmt(rules, 420)
            if args.full_rules:
                print("rules_text:")
                print(_wrap_block(rules_text, width=120))
            else:
                print(f"rules_text: {rules_text}")
        else:
            print("rules_text: <vide>")

        if args.show_math:
            station = _extract_station_from_rules(rules)
            date_from_rules = _extract_date_from_rules(rules)
            variable_from_rules = _extract_variable_from_rules(rules)
            unit_from_rules = _extract_unit_from_rules(rules)
            regex_rule = _math_rule_from_question(question)
            if not regex_rule.get("date") and date_from_rules:
                regex_rule["date"] = date_from_rules
            if regex_rule.get("variable") in ("unknown", "temp") and variable_from_rules != "unknown":
                regex_rule["variable"] = variable_from_rules
            if regex_rule.get("unit") == "unknown" and unit_from_rules != "unknown":
                regex_rule["unit"] = unit_from_rules
            after_regex = _render_math_rule(regex_rule, station=station, source=final_source)

            print("step_math.AVANT_paragraphe:")
            print(_wrap_block(rules if has_rules else "<vide>", width=120))
            print("step_math.APRES_regex_json:")
            print(json.dumps(regex_rule, ensure_ascii=False, indent=2))
            print("step_math.APRES_regex_formule:")
            print(after_regex)

            if args.with_llm and has_rules and llm_needed:
                llm_math = _llm_math_from_rules(question, rules)
                llm_source_name = str(llm_math.get("source_name", "unknown"))
                after_llm = _render_math_rule(regex_rule, station=station, source=llm_source_name)
                print("step_math.APRES_llm_json:")
                print(json.dumps(llm_math, ensure_ascii=False, indent=2))
                print("step_math.APRES_llm_formule:")
                print(after_llm)
        print()

        if args.dump_file:
            dump_lines.append("-" * 110)
            dump_lines.append(f"Q: {question}")
            dump_lines.append(f"condition_id: {condition_id}")
            dump_lines.append(f"gamma.resolutionSource: {resolution_source or 'N/A'}")
            dump_lines.append(f"step2.source_from_rules: {rules_source}")
            dump_lines.append(f"step2.source_from_gamma_resolutionSource: {gamma_source_name}")
            dump_lines.append(f"step2.urls_from_rules: {len(rules_urls)}")
            if rules_urls:
                dump_lines.append(f"step2.first_url_from_rules: {rules_urls[0]}")
            dump_lines.append("rules_text_full:")
            dump_lines.append(rules if has_rules else "<vide>")
            dump_lines.append("")

    print("=" * 110)
    print("RESUME")
    print(f"marches meteo inspectes: {weather_total}")
    print(f"marches affiches        : {displayed_total}")
    print(f"avec rules detectees   : {weather_with_rules}")
    print(f"sans rules detectees   : {weather_without_rules}")
    if args.with_llm:
        print(f"llm_mode=model/cache   : {llm_model_calls}")
        print(f"llm_mode=fallback      : {llm_fallback_calls}")
    print("=" * 110)

    if args.dump_file:
        out_path = args.dump_file
        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(dump_lines))
        print(f"Dump ecrit: {out_path}")


if __name__ == "__main__":
    main()
