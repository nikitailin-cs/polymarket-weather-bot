"""
Récupère les vraies températures historiques pour tous les marchés résolus.
Source : Open-Meteo Archive API (gratuit, sans clé, depuis 1940).
"""
import os, sys, json, urllib.request, urllib.parse, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

ROOT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MASTER  = os.path.join(ROOT, "data", "raw", "master_weather.json")
OUT     = os.path.join(ROOT, "data", "raw", "historical_weather_actuals.json")
CACHE   = os.path.join(ROOT, "data", "raw", "weather_actuals_cache.json")

# Coordonnées des villes connues
CITY_COORDS = {
    "london":        (51.5074, -0.1278),
    "paris":         (48.8566,  2.3522),
    "madrid":        (40.4168, -3.7038),
    "berlin":        (52.5200, 13.4050),
    "rome":          (41.9028, 12.4964),
    "milan":         (45.4654,  9.1859),
    "munich":        (48.1351, 11.5820),
    "amsterdam":     (52.3676,  4.9041),
    "brussels":      (50.8503,  4.3517),
    "vienna":        (48.2082, 16.3738),
    "warsaw":        (52.2297, 21.0122),
    "stockholm":     (59.3293, 18.0686),
    "oslo":          (59.9139, 10.7522),
    "copenhagen":    (55.6761, 12.5683),
    "helsinki":      (60.1699, 24.9384),
    "athens":        (37.9838, 23.7275),
    "istanbul":      (41.0082, 28.9784),
    "ankara":        (39.9334, 32.8597),
    "moscow":        (55.7558, 37.6173),
    "kyiv":          (50.4501, 30.5234),
    "new york":      (40.7128, -74.0060),
    "new york city": (40.7128, -74.0060),
    "nyc":           (40.7128, -74.0060),
    "los angeles":   (34.0522,-118.2437),
    "chicago":       (41.8781, -87.6298),
    "houston":       (29.7604, -95.3698),
    "miami":         (25.7617, -80.1918),
    "dallas":        (32.7767, -96.7970),
    "atlanta":       (33.7490, -84.3880),
    "seattle":       (47.6062,-122.3321),
    "san francisco": (37.7749,-122.4194),
    "denver":        (39.7392,-104.9903),
    "boston":        (42.3601, -71.0589),
    "toronto":       (43.6532, -79.3832),
    "montreal":      (45.5017, -73.5673),
    "vancouver":     (49.2827,-123.1207),
    "mexico city":   (19.4326, -99.1332),
    "buenos aires":  (-34.6037, -58.3816),
    "sao paulo":     (-23.5505, -46.6333),
    "rio de janeiro":(-22.9068, -43.1729),
    "bogota":        ( 4.7110, -74.0721),
    "lima":          (-12.0464, -77.0428),
    "santiago":      (-33.4489, -70.6693),
    "tokyo":         (35.6762, 139.6503),
    "beijing":       (39.9042, 116.4074),
    "shanghai":      (31.2304, 121.4737),
    "hong kong":     (22.3193, 114.1694),
    "singapore":     ( 1.3521, 103.8198),
    "seoul":         (37.5665, 126.9780),
    "taipei":        (25.0330, 121.5654),
    "bangkok":       (13.7563, 100.5018),
    "jakarta":       (-6.2088, 106.8456),
    "mumbai":        (19.0760,  72.8777),
    "delhi":         (28.6139,  77.2090),
    "karachi":       (24.8607,  67.0011),
    "dubai":         (25.2048,  55.2708),
    "riyadh":        (24.7136,  46.6753),
    "tel aviv":      (32.0853,  34.7818),
    "cairo":         (30.0444,  31.2357),
    "lagos":         ( 6.5244,   3.3792),
    "nairobi":       (-1.2921,  36.8219),
    "johannesburg":  (-26.2041,  28.0473),
    "sydney":        (-33.8688, 151.2093),
    "melbourne":     (-37.8136, 144.9631),
    "auckland":      (-36.8485, 174.7633),
    "wellington":    (-41.2866, 174.7756),
    "wuhan":         (30.5928, 114.3055),
    "chengdu":       (30.5728, 104.0668),
    "shenzhen":      (22.5431, 114.0579),
    "chongqing":     (29.4316, 106.9123),
}

def get_coords(city: str):
    """Retourne (lat, lon) pour une ville."""
    key = city.lower().strip()
    if key in CITY_COORDS:
        return CITY_COORDS[key]
    # Essai partiel
    for k, v in CITY_COORDS.items():
        if k in key or key in k:
            return v
    return None

def fetch_actual_temp(lat, lon, date_str):
    """
    Récupère la température max réelle via Open-Meteo Archive.
    date_str format : YYYY-MM-DD
    """
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={date_str}&end_date={date_str}"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
        f"&timezone=UTC"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    daily = data.get("daily", {})
    temps_max  = daily.get("temperature_2m_max", [None])
    temps_min  = daily.get("temperature_2m_min", [None])
    precip     = daily.get("precipitation_sum", [None])
    return {
        "temp_max":  temps_max[0] if temps_max else None,
        "temp_min":  temps_min[0] if temps_min else None,
        "precip_mm": precip[0]    if precip    else None,
    }

# ── Charge le cache existant ─────────────────────────────
if os.path.exists(CACHE):
    with open(CACHE, encoding="utf-8") as f:
        cache = json.load(f)
    print(f"📂 Cache existant : {len(cache):,} entrées")
else:
    cache = {}
    print("📂 Nouveau cache")

# ── Charge le master ─────────────────────────────────────
print("📂 Chargement master...")
with open(MASTER, encoding="utf-8") as f:
    master = json.load(f)

# Filtre : marchés température résolus avec city parsée
targets = [
    m for m in master
    if m.get("resolved") is True
    and m.get("city")
    and m.get("end_date")
    and m.get("target_c") is not None
]
print(f"   {len(targets):,} marchés température résolus avec city\n")

# ── Fetch les températures réelles ───────────────────────
results  = []
errors   = 0
skipped  = 0
fetched  = 0

print("🌡️  Récupération températures réelles...\n")

for i, m in enumerate(targets):
    city     = m["city"]
    date_str = m["end_date"][:10]
    cid      = m["condition_id"]
    cache_key = f"{city.lower()}_{date_str}"

    # Skip si déjà en cache
    if cache_key in cache:
        skipped += 1
        act = cache[cache_key]
    else:
        coords = get_coords(city)
        if not coords:
            continue
        lat, lon = coords
        try:
            act = fetch_actual_temp(lat, lon, date_str)
            cache[cache_key] = act
            fetched += 1
            time.sleep(0.1)  # Rate limit
        except Exception as e:
            errors += 1
            continue

    if act.get("temp_max") is None:
        continue

    results.append({
        "condition_id": cid,
        "question":     m.get("question", ""),
        "city":         city,
        "date":         date_str,
        "target_c":     m["target_c"],
        "modifier":     m.get("modifier", "exact"),
        "yes_won":      m.get("yes_won"),
        "yes_price_clob": m.get("yes_price_clob"),
        "actual_temp_max": act["temp_max"],
        "actual_temp_min": act["temp_min"],
        "actual_precip_mm": act["precip_mm"],
        # Vérification : est-ce que le résultat était correct ?
        "actual_yes": (
            act["temp_max"] == m["target_c"]          if m.get("modifier") == "exact"
            else act["temp_max"] <= m["target_c"]      if m.get("modifier") == "below"
            else act["temp_max"] >= m["target_c"]      if m.get("modifier") == "above"
            else None
        ),
    })

    # Sauvegarde cache toutes les 100 entrées
    if (i + 1) % 100 == 0:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        pct = (i+1) / len(targets) * 100
        print(f"  [{i+1:>5}/{len(targets)}] {pct:.1f}% | fetched={fetched} cached={skipped} errors={errors}")

# Sauvegarde finale cache
with open(CACHE, "w", encoding="utf-8") as f:
    json.dump(cache, f)

# ── Sauvegarde résultats ─────────────────────────────────
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, default=str)

# ── Stats ────────────────────────────────────────────────
with_actual = [r for r in results if r["actual_temp_max"] is not None]
correct     = [r for r in with_actual if r["actual_yes"] == r["yes_won"]]

print(f"\n{'═'*60}")
print(f"  TEMPÉRATURES HISTORIQUES RÉELLES")
print(f"{'═'*60}")
print(f"  Marchés cibles            : {len(targets):,}")
print(f"  Fetched (nouveaux)        : {fetched:,}")
print(f"  Depuis cache              : {skipped:,}")
print(f"  Avec temp réelle          : {len(with_actual):,}")
print(f"  Erreurs                   : {errors:,}")
print(f"  ─────────────────────────────────────")
print(f"  Cohérence résultat/météo  : {len(correct)/len(with_actual)*100:.1f}% ← validation")
print(f"{'═'*60}")
print(f"\n💾 {OUT}")