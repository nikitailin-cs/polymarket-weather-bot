"""
NOAA Global Surface Summary of Day (GSOD).
Températures réelles mesurées en stations physiques depuis 1929.
Valide et complète Open-Meteo Archive.
"""
import os, sys, json, urllib.request, time, csv, io, gzip
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

ROOT   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MASTER = os.path.join(ROOT, "data", "raw", "master_weather.json")
OUT    = os.path.join(ROOT, "data", "raw", "noaa_gsod_actuals.json")
CACHE  = os.path.join(ROOT, "data", "raw", "noaa_gsod_cache.json")

# Stations NOAA les plus proches des villes du master
# Format : USAF-WBAN (trouvé sur https://www.ncei.noaa.gov/access/search/data-search/global-summary-of-the-day)
CITY_STATIONS = {
    "london":        "037720-99999",
    "paris":         "071490-99999",
    "madrid":        "082210-99999",
    "berlin":        "103820-99999",
    "rome":          "162420-99999",
    "milan":         "160800-99999",
    "munich":        "108660-99999",
    "amsterdam":     "062400-99999",
    "vienna":        "110350-99999",
    "warsaw":        "123750-99999",
    "stockholm":     "024600-99999",
    "oslo":          "014920-99999",
    "athens":        "166220-99999",
    "istanbul":      "170600-99999",
    "ankara":        "171280-99999",
    "moscow":        "276120-99999",
    "new york":      "725030-14732",
    "new york city": "725030-14732",
    "nyc":           "725030-14732",
    "chicago":       "725300-94846",
    "los angeles":   "722950-23174",
    "miami":         "722020-12839",
    "dallas":        "722590-03927",
    "atlanta":       "722190-13874",
    "seattle":       "727930-24233",
    "san francisco": "724940-23234",
    "denver":        "725650-03017",
    "boston":        "725090-14739",
    "toronto":       "716240-99999",
    "buenos aires":  "875760-99999",
    "sao paulo":     "837800-99999",
    "tokyo":         "476620-99999",
    "beijing":       "545110-99999",
    "shanghai":      "583620-99999",
    "hong kong":     "450070-99999",
    "singapore":     "486980-99999",
    "seoul":         "471080-99999",
    "taipei":        "466600-99999",
    "dubai":         "411940-99999",
    "tel aviv":      "401790-99999",
    "cairo":         "623660-99999",
    "sydney":        "947670-99999",
    "melbourne":     "948590-99999",
    "wellington":    "934360-99999",
}

def fetch_gsod(station_id, year):
    """Récupère les données GSOD via API NCEI services."""
    url = (
        f"https://www.ncei.noaa.gov/access/services/data/v1"
        f"?dataset=global-summary-of-the-day"
        f"&stations={station_id.replace('-','')}"
        f"&startDate={year}-01-01&endDate={year}-12-31"
        f"&format=csv"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            content = r.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(content))
        return list(reader)
    except:
        return []

def get_day_temp(station_id, date_str):
    """Retourne TMAX et TMIN pour une date donnée."""
    year = date_str[:4]
    cache_key = f"gsod_{station_id}_{date_str}"
    
    rows = fetch_gsod(station_id, year)
    for row in rows:
        if row.get("DATE") == date_str:
            tmax_f = row.get("TMAX")
            tmin_f = row.get("TMIN")
            prec   = row.get("PRCP")
            
            # GSOD en Fahrenheit → Celsius
            def f2c(v):
                try:
                    f = float(v)
                    if f == 9999.9:
                        return None
                    return round((f - 32) * 5/9, 1)
                except:
                    return None
            
            return {
                "gsod_tmax": f2c(tmax_f),
                "gsod_tmin": f2c(tmin_f),
                "gsod_prec": float(prec) if prec and prec != "99.99" else None,
                "station":   station_id,
            }
    return {}

# Charge cache et master
cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
print(f"📂 Cache GSOD : {len(cache):,} entrées")

with open(MASTER, encoding="utf-8") as f:
    master = json.load(f)

targets = [
    m for m in master
    if m.get("resolved") is True
    and m.get("city")
    and m.get("end_date")
    and m.get("city", "").lower() in CITY_STATIONS
]
print(f"📂 {len(targets):,} marchés avec station NOAA connue\n")

results  = []
fetched  = errors = cached = 0
year_cache = {}  # Cache des fichiers CSV par station/année

print("🌡️  Récupération NOAA GSOD...\n")

for i, m in enumerate(targets):
    city      = m["city"].lower()
    end_date  = m["end_date"][:10]
    cid       = m["condition_id"]
    station   = CITY_STATIONS[city]
    year      = end_date[:4]
    cache_key = f"gsod_{station}_{end_date}"

    if cache_key in cache:
        data = cache[cache_key]
        cached += 1
    else:
        # Charge le CSV de l'année si pas encore en mémoire
        year_key = f"{station}_{year}"
        if year_key not in year_cache:
            rows = fetch_gsod(station, year)
            year_cache[year_key] = {r.get("DATE"): r for r in rows}
            time.sleep(0.3)
        
        day_data = year_cache[year_key].get(end_date, {})
        
        def f2c(v):
            try:
                f = float(str(v).strip().replace("*", ""))
                return None if f >= 9999.0 else round((f-32)*5/9, 1)
            except:
                return None
        
        data = {
            "gsod_tmax": f2c(day_data.get("MAX")),
            "gsod_tmin": f2c(day_data.get("MIN")),
            "gsod_prec": float(day_data["PRCP"]) if day_data.get("PRCP") not in [None, "99.99"] else None,
            "station":   station,
        }
        cache[cache_key] = data
        fetched += 1

    if data.get("gsod_tmax") is None:
        errors += 1
        continue

    results.append({
        "condition_id":    cid,
        "city":            m["city"],
        "end_date":        end_date,
        "target_c":        m["target_c"],
        "yes_won":         m.get("yes_won"),
        "actual_temp_max": m.get("actual_temp_max"),
        **data,
        # Validation croisée
        "delta_openmeteo_gsod": round(
            (m.get("actual_temp_max") or 0) - (data.get("gsod_tmax") or 0), 1
        ) if m.get("actual_temp_max") and data.get("gsod_tmax") else None,
    })

    if (i + 1) % 50 == 0:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        print(f"  [{i+1:>5}/{len(targets)}] {(i+1)/len(targets)*100:.1f}% | fetch={fetched} cache={cached} err={errors}")

with open(CACHE, "w", encoding="utf-8") as f:
    json.dump(cache, f)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, default=str)

print(f"\n{'═'*60}")
print(f"  NOAA GSOD — RÉSUMÉ")
print(f"{'═'*60}")
print(f"  Marchés avec station      : {len(targets):,}")
print(f"  Avec données GSOD         : {len(results):,}")
print(f"  Erreurs/manquants         : {errors:,}")
if results:
    has_delta = [r for r in results if r["delta_openmeteo_gsod"] is not None]
    if has_delta:
        avg_delta = sum(abs(r["delta_openmeteo_gsod"]) for r in has_delta) / len(has_delta)
        print(f"  Écart moyen Open-Meteo vs GSOD : {avg_delta:.2f}°C")
print(f"{'═'*60}")
print(f"\n💾 {OUT}")