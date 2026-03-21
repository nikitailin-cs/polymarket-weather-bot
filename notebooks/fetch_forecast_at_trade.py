"""
Récupère les prévisions météo qui existaient 24-48h AVANT chaque marché.
C'est ce que le bot aurait vu à l'époque — feature cruciale pour le training IA.
Open-Meteo forecast archive : reconstruit les prévisions historiques.
"""
import os, sys, json, urllib.request, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

ROOT   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MASTER = os.path.join(ROOT, "data", "raw", "master_weather.json")
OUT    = os.path.join(ROOT, "data", "raw", "forecast_at_trade.json")
CACHE  = os.path.join(ROOT, "data", "raw", "forecast_cache.json")

# Coordonnées villes — même dict que fetch_historical_weather.py
CITY_COORDS = {
    "london": (51.5074, -0.1278), "paris": (48.8566, 2.3522),
    "madrid": (40.4168, -3.7038), "berlin": (52.5200, 13.4050),
    "rome": (41.9028, 12.4964), "milan": (45.4654, 9.1859),
    "munich": (48.1351, 11.5820), "amsterdam": (52.3676, 4.9041),
    "vienna": (48.2082, 16.3738), "warsaw": (52.2297, 21.0122),
    "stockholm": (59.3293, 18.0686), "oslo": (59.9139, 10.7522),
    "athens": (37.9838, 23.7275), "istanbul": (41.0082, 28.9784),
    "ankara": (39.9334, 32.8597), "moscow": (55.7558, 37.6173),
    "new york": (40.7128, -74.0060), "new york city": (40.7128, -74.0060),
    "nyc": (40.7128, -74.0060), "los angeles": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298), "houston": (29.7604, -95.3698),
    "miami": (25.7617, -80.1918), "dallas": (32.7767, -96.7970),
    "atlanta": (33.7490, -84.3880), "seattle": (47.6062, -122.3321),
    "san francisco": (37.7749, -122.4194), "denver": (39.7392, -104.9903),
    "boston": (42.3601, -71.0589), "toronto": (43.6532, -79.3832),
    "buenos aires": (-34.6037, -58.3816), "sao paulo": (-23.5505, -46.6333),
    "tokyo": (35.6762, 139.6503), "beijing": (39.9042, 116.4074),
    "shanghai": (31.2304, 121.4737), "hong kong": (22.3193, 114.1694),
    "singapore": (1.3521, 103.8198), "seoul": (37.5665, 126.9780),
    "taipei": (25.0330, 121.5654), "bangkok": (13.7563, 100.5018),
    "mumbai": (19.0760, 72.8777), "delhi": (28.6139, 77.2090),
    "dubai": (25.2048, 55.2708), "tel aviv": (32.0853, 34.7818),
    "cairo": (30.0444, 31.2357), "sydney": (-33.8688, 151.2093),
    "melbourne": (-37.8136, 144.9631), "wellington": (-41.2866, 174.7756),
    "wuhan": (30.5928, 114.3055), "chengdu": (30.5728, 104.0668),
    "shenzhen": (22.5431, 114.0579), "chongqing": (29.4316, 106.9123),
}

def get_coords(city):
    key = city.lower().strip()
    if key in CITY_COORDS:
        return CITY_COORDS[key]
    for k, v in CITY_COORDS.items():
        if k in key or key in k:
            return v
    return None

def fetch_forecast_for_date(lat, lon, target_date, forecast_date):
    """
    Simule la prévision qui existait à forecast_date pour target_date.
    Open-Meteo archive reconstruit les prévisions historiques GFS/ECMWF.
    forecast_date = target_date - 2 jours (ce que le bot aurait vu)
    """
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={forecast_date}&end_date={target_date}"
        f"&daily=temperature_2m_max,temperature_2m_min,"
        f"precipitation_sum,wind_speed_10m_max"
        f"&timezone=UTC"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    tmax  = daily.get("temperature_2m_max", [])
    tmin  = daily.get("temperature_2m_min", [])
    prec  = daily.get("precipitation_sum", [])
    wind  = daily.get("wind_speed_10m_max", [])
    
    # Trouve la ligne correspondant à target_date
    result = {}
    for i, d in enumerate(dates):
        if d == target_date:
            result["forecast_tmax"]  = tmax[i] if i < len(tmax) else None
            result["forecast_tmin"]  = tmin[i] if i < len(tmin) else None
            result["forecast_precip"]= prec[i] if i < len(prec) else None
            result["forecast_wind"]  = wind[i] if i < len(wind) else None
            break
    
    # Trend : évolution des 2 jours précédents
    if len(tmax) >= 2:
        result["trend_2d"] = round(
            (tmax[-1] or 0) - (tmax[0] or 0), 2
        ) if tmax[-1] and tmax[0] else None
    
    return result

# Charge cache
cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
print(f"📂 Cache : {len(cache):,} entrées\n")

# Charge master
with open(MASTER, encoding="utf-8") as f:
    master = json.load(f)

targets = [
    m for m in master
    if m.get("resolved") is True
    and m.get("city")
    and m.get("end_date")
    and m.get("target_c") is not None
]
print(f"📂 {len(targets):,} marchés cibles\n")

results  = []
fetched  = 0
cached   = 0
errors   = 0

print("🌤️  Récupération prévisions historiques...\n")

for i, m in enumerate(targets):
    city      = m["city"]
    end_date  = m["end_date"][:10]
    cid       = m["condition_id"]

    # Prévision simulée = données 2 jours avant la date cible
    from datetime import datetime, timedelta
    try:
        target_dt    = datetime.strptime(end_date, "%Y-%m-%d")
        forecast_dt  = target_dt - timedelta(days=2)
        forecast_date = forecast_dt.strftime("%Y-%m-%d")
    except:
        continue

    cache_key = f"forecast_{city.lower()}_{end_date}"

    if cache_key in cache:
        forecast = cache[cache_key]
        cached += 1
    else:
        coords = get_coords(city)
        if not coords:
            continue
        lat, lon = coords
        try:
            forecast = fetch_forecast_for_date(lat, lon, end_date, forecast_date)
            cache[cache_key] = forecast
            fetched += 1
            time.sleep(0.1)
        except Exception as e:
            errors += 1
            continue

    results.append({
        "condition_id":    cid,
        "city":            city,
        "end_date":        end_date,
        "target_c":        m["target_c"],
        "modifier":        m.get("modifier"),
        "yes_won":         m.get("yes_won"),
        "yes_price_clob":  m.get("yes_price_clob"),
        "actual_temp_max": m.get("actual_temp_max"),
        # Features prévision J-2
        "forecast_tmax":   forecast.get("forecast_tmax"),
        "forecast_tmin":   forecast.get("forecast_tmin"),
        "forecast_precip": forecast.get("forecast_precip"),
        "forecast_wind":   forecast.get("forecast_wind"),
        "trend_2d":        forecast.get("trend_2d"),
        # Feature dérivée clé : écart prévision vs seuil
        "forecast_vs_target": round(
            (forecast.get("forecast_tmax") or 0) - m["target_c"], 2
        ) if forecast.get("forecast_tmax") else None,
    })

    if (i + 1) % 100 == 0:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        pct = (i+1) / len(targets) * 100
        print(f"  [{i+1:>5}/{len(targets)}] {pct:.1f}% | fetch={fetched} cache={cached} err={errors}")

# Sauvegarde finale
with open(CACHE, "w", encoding="utf-8") as f:
    json.dump(cache, f)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, default=str)

print(f"\n{'═'*60}")
print(f"  PRÉVISIONS HISTORIQUES")
print(f"{'═'*60}")
print(f"  Marchés traités           : {len(results):,}")
print(f"  Nouveaux fetches          : {fetched:,}")
print(f"  Depuis cache              : {cached:,}")
print(f"  Erreurs                   : {errors:,}")
if results:
    has_forecast = sum(1 for r in results if r["forecast_tmax"] is not None)
    has_delta    = sum(1 for r in results if r["forecast_vs_target"] is not None)
    print(f"  Avec prévision            : {has_forecast:,}")
    print(f"  Avec écart vs seuil       : {has_delta:,}  ← feature IA principale")
print(f"{'═'*60}")
print(f"\n💾 {OUT}")