"""
Récupère les températures réelles ERA5 pour tous les marchés résolus.
ERA5 = réanalyse ECMWF, résolution 9km, données horaires depuis 1940.
Supérieur à Open-Meteo pour la précision des températures historiques.
"""
import os, sys, json, time
from datetime import datetime, timezone
from collections import defaultdict
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv
load_dotenv()

import cdsapi
import xarray as xr
import numpy as np

ROOT   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MASTER = os.path.join(ROOT, "data", "raw", "master_weather.json")
OUT    = os.path.join(ROOT, "data", "raw", "era5_actuals.json")
CACHE  = os.path.join(ROOT, "data", "raw", "era5_cache.json")
TMP    = os.path.join(ROOT, "data", "raw", "era5_tmp")
os.makedirs(TMP, exist_ok=True)

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

# Init CDS client
ERA5_KEY = os.getenv("CDSAPI_KEY", "")
ERA5_URL = os.getenv("CDSAPI_URL", "https://cds.climate.copernicus.eu/api")
client = cdsapi.Client(url=ERA5_URL, key=ERA5_KEY, quiet=True)

# Charge cache et master
cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
print(f"📂 Cache ERA5 : {len(cache):,} entrées")

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

# Groupe par city+year+month pour batching efficace
# ERA5 est lent à la requête — on batch par mois entier
groups = defaultdict(list)
for m in targets:
    city = m["city"]
    coords = get_coords(city)
    if not coords:
        continue
    date_str = m["end_date"][:10]
    year_month = date_str[:7]  # YYYY-MM
    groups[(city, year_month)].append(m)

print(f"📊 {len(groups)} groupes city+mois à traiter\n")

results = []
batch_done = 0

for (city, year_month), markets in groups.items():
    year, month = year_month.split("-")
    coords = get_coords(city)
    lat, lon = coords
    cache_key = f"era5_{city.lower()}_{year_month}"

    if cache_key in cache:
        month_data = cache[cache_key]
    else:
        # Requête ERA5 pour tout le mois — plus efficace
        tmp_file = os.path.join(TMP, f"{city}_{year_month}.nc")
        try:
            print(f"  ⬇️  ERA5 {city} {year_month}...", end=" ", flush=True)
            
            # Bounding box autour de la ville (±0.5°)
            area = [lat+0.5, lon-0.5, lat-0.5, lon+0.5]
            
            client.retrieve(
                "reanalysis-era5-land",
                {
                    "variable": ["2m_temperature"],
                    "year": year,
                    "month": month,
                    "day": [f"{d:02d}" for d in range(1, 32)],
                    "time": [f"{h:02d}:00" for h in range(24)],
                    "area": area,
                    "data_format": "netcdf",
                    "download_format": "unarchived",
                },
                tmp_file
            )
            
            # Vérifie que le fichier est valide
            if os.path.getsize(tmp_file) < 1000:
                with open(tmp_file, 'rb') as f:
                    print(f"\n   Contenu fichier : {f.read(200)}")
                raise Exception("Fichier trop petit — probablement erreur API")
            
            # Parse NetCDF
            ds = xr.open_dataset(tmp_file, engine="h5netcdf")
            t2m = ds["t2m"] - 273.15  # Kelvin → Celsius
            
            # Calcule tmax journalier
            daily_max = t2m.resample(valid_time="1D").max()
            daily_min = t2m.resample(valid_time="1D").min()
            
            month_data = {}
            for i, date in enumerate(daily_max.valid_time.values):
                date_str = str(date)[:10]
                tmax_val = float(daily_max.isel(valid_time=i).mean().values)
                tmin_val = float(daily_min.isel(valid_time=i).mean().values)
                month_data[date_str] = {
                    "era5_tmax": round(tmax_val, 2),
                    "era5_tmin": round(tmin_val, 2),
                }
            
            ds.close()
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
            
            cache[cache_key] = month_data
            batch_done += 1
            print(f"✅ {len(month_data)} jours")
            
            # Sauvegarde cache toutes les 5 requêtes
            if batch_done % 5 == 0:
                with open(CACHE, "w", encoding="utf-8") as f:
                    json.dump(cache, f)
                    
        except Exception as e:
            print(f"❌ {e}")
            month_data = {}
            cache[cache_key] = month_data

    # Enrichit les marchés avec les données ERA5
    for m in markets:
        date_str = m["end_date"][:10]
        day_data = month_data.get(date_str, {})
        if day_data.get("era5_tmax") is not None:
            results.append({
                "condition_id":   m["condition_id"],
                "city":           city,
                "end_date":       date_str,
                "target_c":       m["target_c"],
                "modifier":       m.get("modifier"),
                "yes_won":        m.get("yes_won"),
                "yes_price_clob": m.get("yes_price_clob"),
                "actual_temp_max":m.get("actual_temp_max"),
                "era5_tmax":      day_data["era5_tmax"],
                "era5_tmin":      day_data["era5_tmin"],
                # Validation vs Open-Meteo
                "delta_era5_openmeteo": round(
                    day_data["era5_tmax"] - (m.get("actual_temp_max") or 0), 2
                ) if m.get("actual_temp_max") else None,
            })

# Sauvegarde finale
with open(CACHE, "w", encoding="utf-8") as f:
    json.dump(cache, f)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, default=str)

print(f"\n{'═'*60}")
print(f"  ERA5 — RÉSUMÉ")
print(f"{'═'*60}")
print(f"  Groupes city+mois traités : {batch_done:,}")
print(f"  Marchés avec ERA5         : {len(results):,}")
if results:
    has_delta = [r for r in results if r["delta_era5_openmeteo"] is not None]
    if has_delta:
        avg = sum(abs(r["delta_era5_openmeteo"]) for r in has_delta)/len(has_delta)
        print(f"  Écart moyen ERA5 vs OM    : {avg:.2f}°C")
print(f"{'═'*60}")
print(f"\n💾 {OUT}")