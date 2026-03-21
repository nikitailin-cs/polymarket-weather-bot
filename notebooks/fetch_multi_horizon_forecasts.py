"""
Prévisions météo à horizons multiples pour chaque marché résolu.
J-1, J-3, J-7 avant expiry → features temporelles pour training IA.
"""
import os, sys, json, urllib.request, time
from datetime import datetime, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

ROOT   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MASTER = os.path.join(ROOT, "data", "raw", "master_weather.json")
OUT    = os.path.join(ROOT, "data", "raw", "multi_horizon_forecasts.json")
CACHE  = os.path.join(ROOT, "data", "raw", "multi_horizon_cache.json")

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

HORIZONS = [1, 2, 3, 7]  # jours avant expiry

def get_coords(city):
    key = city.lower().strip()
    if key in CITY_COORDS:
        return CITY_COORDS[key]
    for k, v in CITY_COORDS.items():
        if k in key or key in k:
            return v
    return None

def fetch_window(lat, lon, start_date, end_date):
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&daily=temperature_2m_max,temperature_2m_min,"
        f"precipitation_sum,wind_speed_10m_max,relative_humidity_2m_max"
        f"&timezone=UTC"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
print(f"📂 Cache : {len(cache):,} entrées")

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

results = []
fetched = errors = cached = 0

for i, m in enumerate(targets):
    city     = m["city"]
    end_date = m["end_date"][:10]
    cid      = m["condition_id"]
    coords   = get_coords(city)
    if not coords:
        continue
    lat, lon = coords

    cache_key = f"mh_{city.lower()}_{end_date}"
    
    if cache_key in cache:
        data = cache[cache_key]
        cached += 1
    else:
        try:
            target_dt  = datetime.strptime(end_date, "%Y-%m-%d")
            start_dt   = target_dt - timedelta(days=max(HORIZONS))
            start_str  = start_dt.strftime("%Y-%m-%d")
            raw = fetch_window(lat, lon, start_str, end_date)
            daily = raw.get("daily", {})
            dates = daily.get("time", [])
            tmax  = daily.get("temperature_2m_max", [])
            tmin  = daily.get("temperature_2m_min", [])
            prec  = daily.get("precipitation_sum", [])
            wind  = daily.get("wind_speed_10m_max", [])
            hum   = daily.get("relative_humidity_2m_max", [])
            
            date_idx = {d: i for i, d in enumerate(dates)}
            data = {}
            for h in HORIZONS:
                d_str = (target_dt - timedelta(days=h)).strftime("%Y-%m-%d")
                idx   = date_idx.get(d_str)
                if idx is not None:
                    data[f"tmax_j{h}"]  = tmax[idx] if idx < len(tmax) else None
                    data[f"tmin_j{h}"]  = tmin[idx] if idx < len(tmin) else None
                    data[f"prec_j{h}"]  = prec[idx] if idx < len(prec) else None
                    data[f"wind_j{h}"]  = wind[idx] if idx < len(wind) else None
                    data[f"hum_j{h}"]   = hum[idx]  if idx < len(hum)  else None
            
            # Trend J-7 → J-1
            t7 = data.get("tmax_j7")
            t1 = data.get("tmax_j1")
            data["trend_7d"] = round(t1 - t7, 2) if t1 and t7 else None
            t3 = data.get("tmax_j3")
            data["trend_3d"] = round(t1 - t3, 2) if t1 and t3 else None

            cache[cache_key] = data
            fetched += 1
            time.sleep(0.1)
        except Exception as e:
            errors += 1
            continue

    rec = {
        "condition_id":   cid,
        "city":           city,
        "end_date":       end_date,
        "target_c":       m["target_c"],
        "modifier":       m.get("modifier"),
        "yes_won":        m.get("yes_won"),
        "yes_price_clob": m.get("yes_price_clob"),
        "actual_temp_max":m.get("actual_temp_max"),
    }
    rec.update(data)
    
    # Features dérivées clés
    for h in HORIZONS:
        tmax_h = data.get(f"tmax_j{h}")
        if tmax_h is not None:
            rec[f"delta_vs_target_j{h}"] = round(tmax_h - m["target_c"], 2)
    
    results.append(rec)

    if (i + 1) % 100 == 0:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        print(f"  [{i+1:>5}/{len(targets)}] {(i+1)/len(targets)*100:.1f}% | fetch={fetched} cache={cached} err={errors}")

with open(CACHE, "w", encoding="utf-8") as f:
    json.dump(cache, f)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, default=str)

print(f"\n{'═'*60}")
print(f"  PRÉVISIONS MULTI-HORIZONS")
print(f"{'═'*60}")
print(f"  Marchés traités     : {len(results):,}")
print(f"  Nouveaux fetches    : {fetched:,}")
print(f"  Depuis cache        : {cached:,}")
print(f"  Erreurs             : {errors:,}")
print(f"  Features par marché : tmax/tmin/prec/wind/hum × {len(HORIZONS)} horizons + trends")
print(f"{'═'*60}")
print(f"\n💾 {OUT}")