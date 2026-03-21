import re
from typing import Optional

WEATHER_KEYWORDS = [
    "rain", "rainfall", "rainy",
    "snow", "snowfall", "blizzard",
    "hail", "sleet", "precipitation",
    "flood", "flooding", "drought",
    "temperature", "temperatures",
    "celsius", "fahrenheit",
    "heat wave", "heatwave", "cold wave",
    "freeze", "frost",
    "hurricane", "typhoon", "cyclone",
    "tornado", "tropical storm",
    "storm", "thunderstorm",
    "wind", "winds", "windspeed",
    "humidity", "named storm", "named storms",
    "atlantic hurricane season",
    "noaa", "national weather",
]

# Patterns structurels des vrais marchés météo
WEATHER_PATTERNS = [
    r"highest temperature in \w+",
    r"lowest temperature in \w+",
    r"temperature in \w+ be \d+",
    r"inches of (rain|snow|precipitation)",
    r"(mm|cm) of (rain|snow|precipitation)",
    r"wind speed",
    r"make landfall",
    r"hurricane season",
    r"named storm",
    r"\d+°[CF]",           # "29°C", "85°F"
    r"mph|km/h|knots",     # unités de vent
    r"inches of precip",
]

# Faux positifs connus — si la question contient un de ces termes
# ET aucun pattern structurel → on rejette
WEATHER_BLACKLIST_PHRASES = [
    "winds of winter",
    "trade winds",       # expression économique
    "second wind",
    "wind energy",
    "wind power",
    "wind farm",
]

def match_keyword(text: str) -> Optional[str]:
    """
    Retourne le keyword météo si la question est un vrai marché météo.
    Logique : keyword présent ET (pattern structurel OU keyword fort)
    """
    s = (text or "").lower()

    # Vérifie d'abord les blacklist phrases
    for phrase in WEATHER_BLACKLIST_PHRASES:
        if phrase in s:
            return None

    # Cherche un keyword météo
    matched_kw = None
    for kw in WEATHER_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", s):
            matched_kw = kw
            break

    if matched_kw is None:
        return None

    # Keywords "forts" — suffisants seuls (très spécifiques à la météo)
    STRONG_KEYWORDS = {
        "hurricane", "typhoon", "cyclone", "tornado", "tropical storm",
        "precipitation", "rainfall", "snowfall", "blizzard", "thunderstorm",
        "celsius", "fahrenheit", "windspeed", "humidity",
        "named storm", "named storms", "atlantic hurricane season",
        "heatwave", "heat wave", "noaa", "national weather",
    }
    if matched_kw in STRONG_KEYWORDS:
        return matched_kw

    # Keywords "faibles" (wind, storm, rain, snow, temperature...)
    # → exiger un pattern structurel en plus
    for pattern in WEATHER_PATTERNS:
        if re.search(pattern, s):
            return matched_kw

    # Keyword faible sans pattern structurel → rejeté
    return None