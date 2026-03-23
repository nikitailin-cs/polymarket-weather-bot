"""
core/contract_parser.py
-----------------------
Parse la question d'un marché Polymarket en ContractSpec.

Deux niveaux (ordre de coût) :
  1. Regex  → ~0ms, couvre ~90% des marchés météo standards
  2. LLM    → fallback si regex insuffisant, résultat mis en cache

Appelé par market_scanner.py sur chaque NOUVEAU marché uniquement.
Les marchés déjà en cache (y compris les rejets) ne déclenchent aucun appel.

Conformité contractuelle Polymarket :
- oracle_rounding = 0  pour température (arrondi à l'entier, règle Polymarket)
- time_window_utc       extrait depuis la question si présent
- oracle_source         distingue wmo_station (Europe/Asie) vs noaa_ghcnd (USA)
"""
from __future__ import annotations

import hashlib
import json, logging, os, re, urllib.request, urllib.error
from typing import Optional

from core.models import ContractSpec, ContractSpecCache, MarketStatus

log = logging.getLogger(__name__)

# ── Table stations connues ─────────────────────────────────────────────────────
CITY_STATION: dict[str, tuple[str, str, float]] = {
    # USA — NOAA GHCND
    "new york":       ("KNYC",  "noaa_ghcnd", 0.95),
    "nyc":            ("KNYC",  "noaa_ghcnd", 0.95),
    "los angeles":    ("KLAX",  "noaa_ghcnd", 0.92),
    "chicago":        ("KORD",  "noaa_ghcnd", 0.92),
    "miami":          ("KMIA",  "noaa_ghcnd", 0.93),
    "houston":        ("KIAH",  "noaa_ghcnd", 0.90),
    "phoenix":        ("KPHX",  "noaa_ghcnd", 0.91),
    "atlanta":        ("KATL",  "noaa_ghcnd", 0.91),
    "dallas":         ("KDFW",  "noaa_ghcnd", 0.90),
    "seattle":        ("KSEA",  "noaa_ghcnd", 0.90),
    "denver":         ("KDEN",  "noaa_ghcnd", 0.90),
    "boston":         ("KBOS",  "noaa_ghcnd", 0.92),
    "san francisco":  ("KSFO",  "noaa_ghcnd", 0.91),
    "las vegas":      ("KLAS",  "noaa_ghcnd", 0.89),
    "minneapolis":    ("KMSP",  "noaa_ghcnd", 0.89),
    "toronto":        ("CYYZ",  "noaa_ghcnd", 0.88),
    # Europe — WMO stations
    "london":         ("03772", "wmo_station", 0.88),
    "paris":          ("07156", "wmo_station", 0.88),
    "berlin":         ("10384", "wmo_station", 0.87),
    "madrid":         ("08221", "wmo_station", 0.87),
    "rome":           ("16240", "wmo_station", 0.85),
    "milan":          ("16080", "wmo_station", 0.85),
    "amsterdam":      ("06240", "wmo_station", 0.86),
    "brussels":       ("06451", "wmo_station", 0.85),
    "vienna":         ("11035", "wmo_station", 0.86),
    "zurich":         ("06670", "wmo_station", 0.86),
    "munich":         ("10866", "wmo_station", 0.86),
    "stockholm":      ("02485", "wmo_station", 0.85),
    "oslo":           ("01492", "wmo_station", 0.84),
    "warsaw":         ("12375", "wmo_station", 0.84),
    "prague":         ("11518", "wmo_station", 0.85),
    "budapest":       ("12843", "wmo_station", 0.84),
    "lisbon":         ("08535", "wmo_station", 0.86),
    "athens":         ("16716", "wmo_station", 0.85),
    "istanbul":       ("17060", "wmo_station", 0.85),
    "ankara":         ("17130", "wmo_station", 0.84),
    "dublin":         ("03969", "wmo_station", 0.84),
    "copenhagen":     ("06180", "wmo_station", 0.85),
    "helsinki":       ("02978", "wmo_station", 0.84),
    "bucharest":      ("15420", "wmo_station", 0.83),
    # Asie — WMO stations
    "tokyo":          ("47662", "wmo_station", 0.87),
    "beijing":        ("54511", "wmo_station", 0.85),
    "shanghai":       ("58367", "wmo_station", 0.85),
    "shenzhen":       ("59493", "wmo_station", 0.83),
    "wuhan":          ("57494", "wmo_station", 0.83),
    "chongqing":      ("57516", "wmo_station", 0.82),
    "xi an":          ("57036", "wmo_station", 0.82),
    "nanjing":        ("58238", "wmo_station", 0.83),
    "tianjin":        ("54527", "wmo_station", 0.83),
    "chengdu":        ("56294", "wmo_station", 0.83),
    "guangzhou":      ("59287", "wmo_station", 0.83),
    "seoul":          ("47108", "wmo_station", 0.86),
    "singapore":      ("48698", "wmo_station", 0.87),
    "mumbai":         ("43003", "wmo_station", 0.84),
    "delhi":          ("42182", "wmo_station", 0.83),
    "new delhi":      ("42182", "wmo_station", 0.83),
    "lucknow":        ("42279", "wmo_station", 0.82),
    "bangkok":        ("48455", "wmo_station", 0.84),
    "jakarta":        ("96749", "wmo_station", 0.82),
    "dubai":          ("41194", "wmo_station", 0.85),
    "karachi":        ("41780", "wmo_station", 0.82),
    "hong kong":      ("45007", "wmo_station", 0.86),
    "taipei":         ("46692", "wmo_station", 0.84),
    "osaka":          ("47772", "wmo_station", 0.86),
    "kuala lumpur":   ("48650", "wmo_station", 0.83),
    "manila":         ("98429", "wmo_station", 0.82),
    # Océanie / Amériques / Afrique
    "sydney":         ("94767", "wmo_station", 0.86),
    "melbourne":      ("94866", "wmo_station", 0.85),
    "buenos aires":   ("87582", "wmo_station", 0.84),
    "sao paulo":      ("83779", "wmo_station", 0.83),
    "rio de janeiro": ("83743", "wmo_station", 0.83),
    "mexico city":    ("76679", "wmo_station", 0.83),
    "cairo":          ("62366", "wmo_station", 0.84),
    "lagos":          ("65201", "wmo_station", 0.80),
    "nairobi":        ("63741", "wmo_station", 0.81),
    "johannesburg":   ("68816", "wmo_station", 0.82),
    "cape town":      ("68816", "wmo_station", 0.82),
    "santiago":       ("85574", "wmo_station", 0.83),
    "lima":           ("84628", "wmo_station", 0.82),
    "bogota":         ("80222", "wmo_station", 0.82),
}

# ── Patterns seuil — formats réels Polymarket ──────────────────────────────────
_THRESHOLD_PATTERNS = [
    (r"\bbe\s+([-+]?\d+(?:\.\d+)?)\s*[°º]?\s*(f|fahrenheit|c|celsius)\s+or\s+(?:higher|above|more)",  "gte"),
    (r"\bbe\s+([-+]?\d+(?:\.\d+)?)\s*[°º]?\s*(f|fahrenheit|c|celsius)\s+or\s+(?:lower|below|less)",   "lte"),
    (r"\bbe\s+([-+]?\d+(?:\.\d+)?)\s*[°º]?\s*(f|fahrenheit|c|celsius)(?:\s+\bon\b|\?)",                "eq"),
    (r"\bbe\s+exactly\s+([-+]?\d+(?:\.\d+)?)\s*[°º]?\s*(f|fahrenheit|c|celsius)",                     "eq"),
    (r"(?:exceed|above|over|surpass|more\s+than)\s+([-+]?\d+(?:\.\d+)?)\s*[°º]?\s*(f|fahrenheit|c|celsius)", "gt"),
    (r"(?:at\s+least|reach|hit|≥|>=)\s+([-+]?\d+(?:\.\d+)?)\s*[°º]?\s*(f|fahrenheit|c|celsius)",    "gte"),
    (r"(?:below|under|less\s+than|<)\s+([-+]?\d+(?:\.\d+)?)\s*[°º]?\s*(f|fahrenheit|c|celsius)",    "lt"),
    (r"temperature.*?([-+]?\d+(?:\.\d+)?)\s*[°º]?\s*(f|fahrenheit|c|celsius)",                          "eq"),
]
_BETWEEN_PATTERNS = [
    r"between\s+([-+]?\d+(?:\.\d+)?)\s*[-–]\s*([-+]?\d+(?:\.\d+)?)\s*[°º]?\s*(f|fahrenheit|c|celsius)",
    r"between\s+([-+]?\d+(?:\.\d+)?)\s+and\s+([-+]?\d+(?:\.\d+)?)\s*[°º]?\s*(f|fahrenheit|c|celsius)",
]

_VARIABLE_KEYWORDS = {
    "temp_max":  ["high temp", "maximum temp", "temp max", "high of", "high above",
                  "high temperature", "highest temperature", "highest temp"],
    "temp_min":  ["low temp", "minimum temp", "temp min", "low of",  "low below",
                  "low temperature",  "lowest temperature",  "lowest temp"],
    "temp_avg":  ["average temp", "mean temp", "avg temp"],
    "precip":    ["rain", "precipitation", "precip", "rainfall", "inches of rain"],
    "snow":      ["snow", "snowfall", "accumulation", "blizzard"],
    "wind_avg":  ["wind speed", "average wind", "sustained wind"],
    "wind_gust": ["wind gust", "gust"],
    "humidity":  ["humidity", "relative humidity"],
}

# oracle_rounding=0 pour température : Polymarket arrondit à l'entier
_ORACLE_ROUNDING: dict[str, int] = {
    "temp_max":  0, "temp_min":  0, "temp_avg":  0,
    "precip":    1, "snow":      1,
    "wind_avg":  0, "wind_gust": 0, "humidity":  0,
}

_DEFAULT_TIME_WINDOW: dict[str, list] = {
    "temp_max":  [0, 23], "temp_min":  [0, 23], "temp_avg":  [0, 23],
    "precip":    [0, 23], "snow":      [0, 23],
    "wind_avg":  [0, 23], "wind_gust": [0, 23], "humidity":  [0, 23],
}


def _detect_variable(ql: str) -> str:
    for var, keywords in _VARIABLE_KEYWORDS.items():
        if any(kw in ql for kw in keywords):
            return var
    return "unknown"


def _detect_unit(s: str) -> str:
    s = s.lower().strip()
    if s in ("f", "fahrenheit", "degrees f"): return "fahrenheit"
    if s in ("c", "celsius",    "degrees c"): return "celsius"
    if "mph" in s:  return "mph"
    if "kmh" in s or "kph" in s: return "kmh"
    if "mm"  in s:  return "mm"
    if "cm"  in s:  return "cm"
    return "unknown"


def _detect_station(ql: str) -> tuple[str, str, float]:
    for city in sorted(CITY_STATION, key=len, reverse=True):
        if city in ql:
            return CITY_STATION[city]
    return ("", "unknown", 0.35)


_RULE_SOURCE_PATTERNS: list[tuple[str, str]] = [
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

_SUPPORTED_ORACLE_SOURCES = {
    "noaa_ghcnd", "wmo_station",
    "wunderground", "weather_com", "accuweather",
    "hk_observatory", "meteo_france", "copernicus",
    "nhc", "nasa_giss",
}
_LLM_RULES_SOURCE_CHOICES = "noaa_ghcnd|wmo_station|nhc|meteo_france|copernicus|weather_com|accuweather|wunderground|hk_observatory|nasa_giss|unknown"
_rules_llm_cache: dict[str, tuple[str, float, str, str, str]] = {}


def _detect_source_from_rules(rules_text: str) -> tuple[str, bool]:
    """
    Retourne (source, explicitement_detectee).
    """
    if not rules_text:
        return "unknown", False
    rl = rules_text.lower()
    for pattern, source in _RULE_SOURCE_PATTERNS:
        if re.search(pattern, rl):
            return source, True
    return "unknown", False


def _extract_first_url(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"https?://[^\s)\]>\"']+", text, flags=re.IGNORECASE)
    if not m:
        return ""
    return m.group(0).rstrip(".,;:!?")


def _source_from_url(url: str) -> str:
    u = (url or "").lower()
    if not u:
        return "unknown"
    if "wunderground.com" in u:
        return "wunderground"
    if "weather.com" in u:
        return "weather_com"
    if "accuweather.com" in u:
        return "accuweather"
    if "weather.gov" in u or "noaa.gov" in u:
        return "noaa_ghcnd"
    if "meteo.france" in u:
        return "meteo_france"
    if "copernicus" in u:
        return "copernicus"
    if "nhc.noaa.gov" in u:
        return "nhc"
    if "weather.gov.hk" in u:
        return "hk_observatory"
    if "giss.nasa.gov" in u:
        return "nasa_giss"
    return "unknown"


def _extract_json_object(text: str) -> Optional[dict]:
    """Parse robuste d'un objet JSON dans une réponse texte."""
    if not text:
        return None
    cleaned = re.sub(r"```json\s*|\s*```", "", text).strip()
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _llm_debug_enabled() -> bool:
    return os.getenv("RULES_LLM_DEBUG", "0").strip() in {"1", "true", "True", "yes", "YES"}


_RULES_SOURCE_SYSTEM = (
    "Task: identify the OFFICIAL resolution weather source from contract rules.\n"
    "Output strict JSON only, no markdown.\n"
    "Priority order: explicit URL/domain in rules > explicit source name in rules > gamma resolutionSource hint.\n"
    f'Schema: {{"source_name":"{_LLM_RULES_SOURCE_CHOICES}","source_url":"","confidence":0.0}}\n'
    "confidence must be 0..1.\n"
    "If source_url is present, it must be a full URL.\n"
    "If unclear, use source_name='unknown'."
)


def _llm_extract_source_from_rules_with_diag(
    rules_text: str,
    gamma_resolution_source: str = "",
) -> tuple[str, float, str, str, str]:
    """
    LLM low-cost fallback pour extraire la source contractuelle depuis les rules.
    Retourne (source_name, confidence, source_url).
    """
    debug = _llm_debug_enabled()
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not rules_text.strip():
        if debug:
            log.warning("[ContractParser][LLM] skip: empty_rules_text")
        return "unknown", 0.0, "", "none", "empty_rules_text"

    cache_key = hashlib.sha1(
        (rules_text.strip() + "||" + (gamma_resolution_source or "").strip()).encode("utf-8")
    ).hexdigest()
    cached = _rules_llm_cache.get(cache_key)
    if cached is not None:
        if debug:
            log.info("[ContractParser][LLM] cache_hit")
        return cached

    max_chars = int(os.getenv("RULES_LLM_MAX_CHARS", "2600"))
    min_conf = float(os.getenv("RULES_LLM_MIN_CONFIDENCE", "0.35"))
    text = rules_text[:max_chars]
    regex_source, _ = _detect_source_from_rules(rules_text)
    regex_url = _extract_first_url(rules_text)
    regex_url_source = _source_from_url(regex_url)
    if not api_key:
        source = regex_source if regex_source != "unknown" else regex_url_source
        if debug:
            log.warning(
                f"[ContractParser][LLM] skip: no_api_key | regex_source={regex_source} "
                f"url_source={regex_url_source} -> {source}"
            )
        out = (source, (0.65 if source != "unknown" else 0.0), regex_url, "fallback_no_key", "no_api_key")
        _rules_llm_cache[cache_key] = out
        return out
    if gamma_resolution_source:
        text += f"\n\nGamma resolutionSource field: {gamma_resolution_source}"
    text += (
        f"\n\nRegex hint source_name: {regex_source}"
        f"\nRegex hint source_url: {regex_url}"
        f"\nRegex hint source_from_url: {regex_url_source}"
    )

    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 120,
        "temperature": 0,
        "system": _RULES_SOURCE_SYSTEM,
        "messages": [{"role": "user", "content": text}],
    }).encode()

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            payload_resp = json.loads(r.read())
        blocks = payload_resp.get("content") or []
        raw_text = "".join(
            str(b.get("text") or "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        )
        d = _extract_json_object(raw_text) or {}
        mode = "model"
        reason = "model_json"
        if debug:
            log.info(
                f"[ContractParser][LLM] response_received chars={len(raw_text)} "
                f"parsed_json={'yes' if bool(d) else 'no'}"
            )
        source = str(d.get("source_name", "unknown")).strip().lower()
        confidence = float(d.get("confidence", 0.0))
        source_url = str(d.get("source_url", "")).strip()
        if confidence == 0.0 and source != "unknown":
            confidence = 0.70
        if not source_url:
            source_url = _extract_first_url(text)
        if not source_url and gamma_resolution_source:
            source_url = str(gamma_resolution_source).strip()
        if source not in _LLM_RULES_SOURCE_CHOICES.split("|"):
            source = "unknown"
        if source == "unknown":
            source = _source_from_url(source_url)
            if source != "unknown":
                mode = "fallback_heuristic"
                reason = "url_domain"
        if source == "unknown" and regex_source != "unknown":
            source = regex_source
            confidence = max(confidence, 0.55)
            mode = "fallback_heuristic"
            reason = "regex_source"
        if source == "unknown" and regex_url_source != "unknown":
            source = regex_url_source
            confidence = max(confidence, 0.60)
            mode = "fallback_heuristic"
            reason = "regex_url_source"
        if confidence < min_conf:
            if debug:
                log.warning(
                    f"[ContractParser][LLM] low_confidence<{min_conf:.2f}: "
                    f"source={source} conf={confidence:.3f} -> unknown"
                )
            source = "unknown"
            mode = "none"
            reason = "low_confidence"
        out = (source, confidence, source_url.rstrip(".,;:!?"), mode, reason)
        _rules_llm_cache[cache_key] = out
        if debug:
            log.info(
                f"[ContractParser][LLM] final source={out[0]} conf={out[1]:.3f} "
                f"url={'set' if bool(out[2]) else 'empty'}"
            )
        return out
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")[:400]
        except Exception:
            pass
        log.warning(f"[ContractParser][LLM] HTTPError {e.code}: {body}")
        return "unknown", 0.0, "", "error", f"http_{e.code}"
    except urllib.error.URLError as e:
        log.warning(f"[ContractParser][LLM] URLError: {e}")
        return "unknown", 0.0, "", "error", "url_error"
    except Exception as e:
        log.warning(f"[ContractParser][LLM] erreur: {e}")
        return "unknown", 0.0, "", "error", "exception"


def _llm_extract_source_from_rules(
    rules_text: str,
    gamma_resolution_source: str = "",
) -> tuple[str, float, str]:
    """
    API rétrocompatible: retourne seulement (source, confidence, url).
    """
    source, conf, url, _mode, _reason = _llm_extract_source_from_rules_with_diag(
        rules_text=rules_text,
        gamma_resolution_source=gamma_resolution_source,
    )
    return source, conf, url


def _regex_parse(question: str, market_id: str, end_date_iso: str) -> Optional[ContractSpec]:
    ql       = question.lower()
    variable = _detect_variable(ql)
    threshold_value, threshold_unit, threshold_op = 0.0, "unknown", "gte"
    threshold_range = None
    matched  = False

    for pattern in _BETWEEN_PATTERNS:
        m = re.search(pattern, ql)
        if m:
            try:
                low = float(m.group(1))
                high = float(m.group(2))
                if low > high:
                    low, high = high, low
                threshold_range = [low, high]
                threshold_value = high
                threshold_unit = _detect_unit(m.group(3))
                threshold_op = "between"
                matched = True
                break
            except (ValueError, IndexError):
                continue

    if not matched:
        for pattern, op in _THRESHOLD_PATTERNS:
            m = re.search(pattern, ql)
            if m:
                try:
                    threshold_value = float(m.group(1))
                    threshold_unit  = _detect_unit(m.group(2))
                    threshold_op    = op
                    matched = True
                    break
                except (ValueError, IndexError):
                    continue

    if not matched or variable == "unknown":
        return None

    station_id, oracle_source, alignment = _detect_station(ql)
    city = next(
        (c.title() for c in sorted(CITY_STATION, key=len, reverse=True) if c in ql),
        ""
    )

    rounding    = _ORACLE_ROUNDING.get(variable, 0)
    time_window = _DEFAULT_TIME_WINDOW.get(variable)

    # Sur Polymarket, les marchés météo sont quasi-exclusivement résolus via UMA.
    # Le champ resolutionSource de Gamma confirme cela, mais on peut le déduire
    # dès le parsing : tout marché météo non-financier → UMA par défaut.
    resolution_source = "uma"

    return ContractSpec(
        market_id              = market_id,
        condition_id           = "",
        question               = question,
        city                   = city,
        weather_variable       = variable,
        threshold_value        = threshold_value,
        threshold_unit         = threshold_unit,
        threshold_op           = threshold_op,
        threshold_range        = threshold_range,
        time_window_utc        = time_window,
        oracle_station_id      = station_id,
        oracle_source          = oracle_source,
        oracle_rounding        = rounding,
        oracle_alignment_score = alignment if station_id else 0.35,
        resolution_source      = resolution_source,
        end_date_iso           = end_date_iso,
        status                 = MarketStatus.ACTIVE,
    )


# ── Prompt LLM ────────────────────────────────────────────────────────────────

_SYSTEM = (
    "Extract contractual fields from a Polymarket weather question. "
    "Reply ONLY with valid JSON, no markdown, no explanation.\n"
    'Schema: {"city":"","country":"","weather_variable":"temp_max|temp_min|temp_avg|precip|snow|wind_avg|unknown",'
    '"threshold_value":0,"threshold_unit":"celsius|fahrenheit|mm|cm|kmh|mph|unknown",'
    '"threshold_op":"gt|gte|lt|lte|eq|between","threshold_range":[0,0],'
    '"oracle_station_id":"","oracle_source":"noaa_ghcnd|wmo_station|unknown",'
    '"oracle_alignment_score":0.0,"confidence":0.0}'
)


def _llm_parse(question: str, market_id: str, end_date_iso: str) -> Optional[ContractSpec]:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    payload = json.dumps({
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": 250,
        "system":     _SYSTEM,
        "messages":   [{"role": "user", "content": f"Question: {question}"}],
    }).encode()

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data    = payload,
            headers = {
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            text = json.loads(r.read())["content"][0]["text"].strip()

        text = re.sub(r"```json\s*|\s*```", "", text).strip()
        d    = json.loads(text)

        if float(d.get("confidence", 0)) < 0.5:
            return None

        city_key = d.get("city", "").lower()
        if city_key in CITY_STATION:
            sid, src, align = CITY_STATION[city_key]
        else:
            sid   = d.get("oracle_station_id", "")
            src   = d.get("oracle_source", "unknown")
            align = float(d.get("oracle_alignment_score", 0.4))

        variable    = d.get("weather_variable", "unknown")
        parsed_op   = d.get("threshold_op", "gte")
        parsed_range = d.get("threshold_range")
        range_value = None
        if isinstance(parsed_range, list) and len(parsed_range) == 2:
            try:
                low = float(parsed_range[0])
                high = float(parsed_range[1])
                if low > high:
                    low, high = high, low
                range_value = [low, high]
            except (TypeError, ValueError):
                range_value = None
        rounding    = _ORACLE_ROUNDING.get(variable, 0)
        time_window = _DEFAULT_TIME_WINDOW.get(variable)

        threshold_value = float(d.get("threshold_value", 0))
        if parsed_op == "between" and range_value is not None:
            threshold_value = float(range_value[1])

        return ContractSpec(
            market_id              = market_id,
            condition_id           = "",
            question               = question,
            city                   = d.get("city", ""),
            country                = d.get("country", ""),
            weather_variable       = variable,
            threshold_value        = threshold_value,
            threshold_unit         = d.get("threshold_unit", "unknown"),
            threshold_op           = parsed_op,
            threshold_range        = range_value,
            time_window_utc        = time_window,
            oracle_station_id      = sid,
            oracle_source          = src,
            oracle_rounding        = rounding,
            oracle_alignment_score = align,
            resolution_source      = "uma",   # tous les marchés météo Polymarket → UMA
            end_date_iso           = end_date_iso,
            status                 = MarketStatus.ACTIVE,
        )

    except Exception as e:
        log.warning(f"[ContractParser] LLM erreur pour {market_id[:12]}: {e}")
        return None


# ── Point d'entrée public ─────────────────────────────────────────────────────

def parse_contract(
    market_id:         str,
    condition_id:      str,
    question:          str,
    end_date_iso:      str,
    cache:             ContractSpecCache,
    force:             bool = False,
    resolution_source: str  = "",    # passé depuis Gamma (resolutionSource)
    liquidity_usd:     float = 0.0,  # passé depuis Gamma (liquidityNum)
    resolution_rules_text: str = "", # passé depuis Gamma (resolution rules)
) -> Optional[ContractSpec]:
    """
    Retourne le ContractSpec du marché, depuis le cache si possible.

    - Cache hit (score OK)    → retour immédiat, 0 réseau
    - Cache hit (rejet connu) → retour None immédiat, 0 LLM  ← BUG FIXÉ
    - Cache miss              → regex, puis LLM si insuffisant
    - Score < MIN_ORACLE_ALIGNMENT → retourne None + met en cache le rejet
    """
    min_align = float(os.getenv("MIN_ORACLE_ALIGNMENT", "0.70"))

    # ── Cache hit ──────────────────────────────────────────────────────────────
    if not force:
        cached = cache.get(market_id)
        if cached is not None:
            # Marché déjà écarté lors d'un cycle précédent → skip silencieux
            if cached.oracle_alignment_score < min_align:
                return None
            return cached if cached.is_tradeable else None

    log.info(f"[ContractParser] Parse: {market_id[:14]} | {question[:55]}")

    # ── Regex ──────────────────────────────────────────────────────────────────
    spec   = _regex_parse(question, market_id, end_date_iso)
    method = "regex"

    # ── Fallback LLM ──────────────────────────────────────────────────────────
    if spec is None or spec.oracle_alignment_score < min_align:
        llm = _llm_parse(question, market_id, end_date_iso)
        if llm is not None:
            spec   = llm
            method = "llm"

    # ── Rejet : TOUJOURS mettre en cache pour éviter de re-tenter ─────────────
    if spec is None or spec.oracle_alignment_score < min_align:
        score = spec.oracle_alignment_score if spec else 0.0
        log.warning(f"[ContractParser] Écarté {market_id[:14]} score={score:.2f} < {min_align}")

        # Crée un stub minimal ou utilise le spec partiel pour mémoriser le rejet.
        # Ainsi, aux cycles suivants : cache hit → return None immédiat, 0 LLM.
        stub = spec if spec is not None else ContractSpec(
            market_id              = market_id,
            condition_id           = condition_id,
            question               = question,
            oracle_alignment_score = 0.0,
            status                 = MarketStatus.ACTIVE,
        )
        stub.condition_id = condition_id
        cache.set(stub)
        return None

    # ── Succès ─────────────────────────────────────────────────────────────────
    spec.condition_id = condition_id

    # Source contractuelle explicite : regex d'abord, LLM low-cost en fallback
    # uniquement si ambigu (source inconnue ou conflit rules vs resolutionSource).
    explicit_oracle_source, has_explicit_source = _detect_source_from_rules(resolution_rules_text)
    gamma_source_hint, gamma_has_hint = _detect_source_from_rules(resolution_source)
    if gamma_has_hint and not has_explicit_source:
        explicit_oracle_source, has_explicit_source = gamma_source_hint, True

    conflict = (
        has_explicit_source and gamma_has_hint and
        explicit_oracle_source != gamma_source_hint
    )
    llm_rules_fallback = os.getenv("ENABLE_RULES_LLM_FALLBACK", "1").strip() not in {"0", "false", "False"}
    if llm_rules_fallback and (conflict or not has_explicit_source):
        llm_source, llm_conf, llm_url, llm_mode, llm_reason = _llm_extract_source_from_rules_with_diag(
            resolution_rules_text,
            gamma_resolution_source=resolution_source,
        )
        if llm_source != "unknown":
            explicit_oracle_source = llm_source
            has_explicit_source = True
            log.info(
                f"[ContractParser] source_from_rules=llm {market_id[:14]} -> "
                f"{llm_source} conf={llm_conf:.2f} mode={llm_mode}/{llm_reason} "
                f"url={llm_url or '-'}"
            )

    # Étape 4 (hard guard): si le contrat expose des rules mais qu'aucune source
    # contractuelle n'est reconnue, on skip pour éviter tout trade aveugle.
    if resolution_rules_text.strip() and not has_explicit_source:
        spec.oracle_source = "unknown"
        spec.oracle_alignment_score = 0.0
        cache.set(spec)
        log.warning(
            f"[ContractParser] Skip {market_id[:14]}: source contractuelle introuvable dans rules"
        )
        return None

    if has_explicit_source:
        spec.oracle_source = explicit_oracle_source
        if explicit_oracle_source in _SUPPORTED_ORACLE_SOURCES:
            spec.oracle_alignment_score = max(spec.oracle_alignment_score, 0.90)
        else:
            spec.oracle_alignment_score = 0.0
            cache.set(spec)
            log.warning(
                f"[ContractParser] Skip {market_id[:14]}: source contractuelle non supportée "
                f"({explicit_oracle_source})"
            )
            return None

    # Override resolution_source si Gamma le fournit explicitement
    # Valeurs connues : "uma", "manual", "admin", "pythia"
    if resolution_source:
        rs = resolution_source.lower()
        if "uma" in rs or "optimistic" in rs:
            spec.resolution_source = "uma"
        elif "pyth" in rs:
            spec.resolution_source = "pyth"
        elif "manual" in rs or "admin" in rs:
            spec.resolution_source = "manual"
        # sinon on garde "uma" par défaut (marchés météo)

    cache.set(spec)

    log.info(
        f"[ContractParser] OK ({method}) | {spec.weather_variable} {spec.threshold_op} "
        f"{spec.threshold_value}{spec.threshold_unit} | "
        f"station={spec.oracle_station_id} rounding={spec.oracle_rounding} "
        f"window={spec.time_window_utc} score={spec.oracle_alignment_score:.2f}"
    )
    return spec