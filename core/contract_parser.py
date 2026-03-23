"""
core/contract_parser.py
-----------------------
Parse la question d'un marché Polymarket en ContractSpec.

Deux niveaux (ordre de coût) :
  1. Regex  → ~0ms, couvre ~70% des marchés météo standards
  2. LLM    → fallback si regex insuffisant, ~$0.0003/marché, résultat mis en cache

Appelé par market_scanner.py sur chaque NOUVEAU marché uniquement.
Les marchés déjà en cache ne déclenchent aucun appel.
"""
from __future__ import annotations

import json, logging, os, re, urllib.request
from typing import Optional

from core.models import ContractSpec, ContractSpecCache, MarketStatus

log = logging.getLogger(__name__)

# ── Table stations connues ─────────────────────────────────────────────────────
# city_name_lowercase → (station_id, oracle_source_str, alignment_score)
CITY_STATION: dict[str, tuple[str, str, float]] = {
    # USA
    "new york":    ("KNYC", "noaa_ghcnd", 0.95),
    "nyc":         ("KNYC", "noaa_ghcnd", 0.95),
    "los angeles": ("KLAX", "noaa_ghcnd", 0.92),
    "chicago":     ("KORD", "noaa_ghcnd", 0.92),
    "miami":       ("KMIA", "noaa_ghcnd", 0.93),
    "houston":     ("KIAH", "noaa_ghcnd", 0.90),
    "phoenix":     ("KPHX", "noaa_ghcnd", 0.91),
    "atlanta":     ("KATL", "noaa_ghcnd", 0.91),
    "dallas":      ("KDFW", "noaa_ghcnd", 0.90),
    "seattle":     ("KSEA", "noaa_ghcnd", 0.90),
    "denver":      ("KDEN", "noaa_ghcnd", 0.90),
    "boston":      ("KBOS", "noaa_ghcnd", 0.92),
    "toronto":     ("CYYZ", "noaa_ghcnd", 0.88),
    # Europe
    "london":      ("03772", "wmo_station", 0.88),
    "paris":       ("07156", "wmo_station", 0.88),
    "berlin":      ("10384", "wmo_station", 0.87),
    "madrid":      ("08221", "wmo_station", 0.87),
    "rome":        ("16240", "wmo_station", 0.85),
    "milan":       ("16080", "wmo_station", 0.85),
    "amsterdam":   ("06240", "wmo_station", 0.86),
    "brussels":    ("06451", "wmo_station", 0.85),
    "vienna":      ("11035", "wmo_station", 0.86),
    "zurich":      ("06670", "wmo_station", 0.86),
    "munich":      ("10866", "wmo_station", 0.86),
    "stockholm":   ("02485", "wmo_station", 0.85),
    "oslo":        ("01492", "wmo_station", 0.84),
    "warsaw":      ("12375", "wmo_station", 0.84),
    "prague":      ("11518", "wmo_station", 0.85),
    "budapest":    ("12843", "wmo_station", 0.84),
    "lisbon":      ("08535", "wmo_station", 0.86),
    "athens":      ("16716", "wmo_station", 0.85),
    "istanbul":    ("17060", "wmo_station", 0.85),
    "ankara":      ("17130", "wmo_station", 0.84),
    # Asie
    "tokyo":       ("47662", "wmo_station", 0.87),
    "beijing":     ("54511", "wmo_station", 0.85),
    "shanghai":    ("58367", "wmo_station", 0.85),
    "shenzhen":    ("59493", "wmo_station", 0.83),
    "wuhan":       ("57494", "wmo_station", 0.83),
    "chengdu":     ("56294", "wmo_station", 0.83),
    "seoul":       ("47108", "wmo_station", 0.86),
    "singapore":   ("48698", "wmo_station", 0.87),
    "mumbai":      ("43003", "wmo_station", 0.84),
    "delhi":       ("42182", "wmo_station", 0.83),
    "lucknow":     ("42279", "wmo_station", 0.82),
    "bangkok":     ("48455", "wmo_station", 0.84),
    "jakarta":     ("96749", "wmo_station", 0.82),
    "dubai":       ("41194", "wmo_station", 0.85),
    "karachi":     ("41780", "wmo_station", 0.82),
    # Océanie / Amériques / Afrique
    "sydney":      ("94767", "wmo_station", 0.86),
    "melbourne":   ("94866", "wmo_station", 0.85),
    "buenos aires":("87582", "wmo_station", 0.84),
    "sao paulo":   ("83779", "wmo_station", 0.83),
    "rio de janeiro":("83743","wmo_station", 0.83),
    "mexico city": ("76679", "wmo_station", 0.83),
    "cairo":       ("62366", "wmo_station", 0.84),
    "lagos":       ("65201", "wmo_station", 0.80),
    "nairobi":     ("63741", "wmo_station", 0.81),
    "johannesburg":("68816", "wmo_station", 0.82),
}

# ── Patterns regex — couvrent les formats réels Polymarket ────────────────────
# Format principal : "be 8°C on", "be 31°C or higher", "be 92°F or higher"
# Format secondaire : "exceed 90°F", "above 35°C", "below 32°F"
_THRESHOLD_PATTERNS = [
    # "be 31°C or higher" / "be 92°F or higher" / "be 15°C or above"
    (r"be\s+([\d.]+)\s*[°º]?\s*(f|fahrenheit|c|celsius)\s+or\s+(?:higher|above|more)",  "gte"),
    # "be 6°C or lower" / "be 32°F or below" / "be 35°C or less"
    (r"be\s+([\d.]+)\s*[°º]?\s*(f|fahrenheit|c|celsius)\s+or\s+(?:lower|below|less)",   "lte"),
    # "be 8°C on March" (exact, le plus courant sur Polymarket)
    (r"be\s+([\d.]+)\s*[°º]?\s*(f|fahrenheit|c|celsius)(?:\s+on\b|\?)",                "eq"),
    # "be exactly 8°C"
    (r"be\s+exactly\s+([\d.]+)\s*[°º]?\s*(f|fahrenheit|c|celsius)",                      "eq"),
    # Formats secondaires anglophones
    (r"(?:exceed|above|over|surpass|more\s+than)\s+([\d.]+)\s*[°º]?\s*(f|fahrenheit|c|celsius)", "gt"),
    (r"(?:at\s+least|reach|hit|≥|>=)\s+([\d.]+)\s*[°º]?\s*(f|fahrenheit|c|celsius)",    "gte"),
    (r"(?:below|under|less\s+than|<)\s+([\d.]+)\s*[°º]?\s*(f|fahrenheit|c|celsius)",    "lt"),
    # "high temperature ... 35°C" (fallback large)
    (r"temperature.*?([\d.]+)\s*[°º]?\s*(f|fahrenheit|c|celsius)",                          "eq"),
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
    for city, info in CITY_STATION.items():
        if city in ql:
            return info
    return ("", "unknown", 0.35)


def _regex_parse(question: str, market_id: str, end_date_iso: str) -> Optional[ContractSpec]:
    ql       = question.lower()
    variable = _detect_variable(ql)
    threshold_value, threshold_unit, threshold_op = 0.0, "unknown", "gte"
    matched  = False

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
    city = next((c.title() for c in CITY_STATION if c in ql), "")

    return ContractSpec(
        market_id              = market_id,
        condition_id           = "",
        question               = question,
        city                   = city,
        weather_variable       = variable,
        threshold_value        = threshold_value,
        threshold_unit         = threshold_unit,
        threshold_op           = threshold_op,
        oracle_station_id      = station_id,
        oracle_source          = oracle_source,
        oracle_rounding        = 1,
        oracle_alignment_score = alignment if station_id else 0.35,
        end_date_iso           = end_date_iso,
        status                 = MarketStatus.ACTIVE,
    )


# ── Prompt LLM (Haiku — minimal) ──────────────────────────────────────────────

_SYSTEM = (
    "Extract contractual fields from a Polymarket weather question. "
    "Reply ONLY with valid JSON, no markdown, no explanation.\n"
    'Schema: {"city":"","country":"","weather_variable":"temp_max|temp_min|temp_avg|precip|snow|wind_avg|unknown",'
    '"threshold_value":0,"threshold_unit":"celsius|fahrenheit|mm|cm|kmh|mph|unknown",'
    '"threshold_op":"gt|gte|lt|lte","oracle_station_id":"","oracle_source":"noaa_ghcnd|wmo_station|unknown",'
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

        # Surcharge depuis la table locale si on a mieux
        city_key = d.get("city", "").lower()
        if city_key in CITY_STATION:
            sid, src, align = CITY_STATION[city_key]
        else:
            sid   = d.get("oracle_station_id", "")
            src   = d.get("oracle_source", "unknown")
            align = float(d.get("oracle_alignment_score", 0.4))

        return ContractSpec(
            market_id              = market_id,
            condition_id           = "",
            question               = question,
            city                   = d.get("city", ""),
            country                = d.get("country", ""),
            weather_variable       = d.get("weather_variable", "unknown"),
            threshold_value        = float(d.get("threshold_value", 0)),
            threshold_unit         = d.get("threshold_unit", "unknown"),
            threshold_op           = d.get("threshold_op", "gte"),
            oracle_station_id      = sid,
            oracle_source          = src,
            oracle_rounding        = 1,
            oracle_alignment_score = align,
            end_date_iso           = end_date_iso,
            status                 = MarketStatus.ACTIVE,
        )

    except Exception as e:
        log.warning(f"[ContractParser] LLM erreur pour {market_id[:12]}: {e}")
        return None


# ── Point d'entrée public ─────────────────────────────────────────────────────

def parse_contract(
    market_id:    str,
    condition_id: str,
    question:     str,
    end_date_iso: str,
    cache:        ContractSpecCache,
    force:        bool = False,
) -> Optional[ContractSpec]:
    """
    Retourne le ContractSpec du marché, depuis le cache si possible.

    - Cache hit  → retour immédiat, 0 réseau
    - Cache miss → regex, puis LLM si insuffisant
    - Score < MIN_ORACLE_ALIGNMENT → retourne None (marché écarté)
    """
    min_align = float(os.getenv("MIN_ORACLE_ALIGNMENT", "0.70"))

    # Cache hit
    if not force:
        cached = cache.get(market_id)
        if cached is not None:
            return cached if cached.is_tradeable else None

    log.info(f"[ContractParser] Parse: {market_id[:14]} | {question[:55]}")

    # Regex
    spec   = _regex_parse(question, market_id, end_date_iso)
    method = "regex"

    # Fallback LLM
    if spec is None or spec.oracle_alignment_score < min_align:
        llm = _llm_parse(question, market_id, end_date_iso)
        if llm is not None:
            spec   = llm
            method = "llm"

    # Filtre final
    if spec is None or spec.oracle_alignment_score < min_align:
        score = spec.oracle_alignment_score if spec else 0.0
        log.warning(f"[ContractParser] Écarté {market_id[:14]} score={score:.2f} < {min_align}")
        return None

    spec.condition_id = condition_id
    cache.set(spec)

    log.info(
        f"[ContractParser] OK ({method}) | {spec.weather_variable} {spec.threshold_op} "
        f"{spec.threshold_value}{spec.threshold_unit} | "
        f"station={spec.oracle_station_id} score={spec.oracle_alignment_score:.2f}"
    )
    return spec