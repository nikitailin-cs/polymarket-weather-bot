"""
Récupère les stats climatologiques pour chaque ville/mois du master.
Utilise climate_history.py existant.
"""
import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from signals.sources.climate_history import get_climate_stats
from datetime import datetime

ROOT   = os.path.join(os.path.dirname(__file__), "..")
MASTER = os.path.join(ROOT, "data", "raw", "master_weather.json")
OUT    = os.path.join(ROOT, "data", "raw", "climate_stats.json")
CACHE  = os.path.join(ROOT, "data", "raw", "climate_stats_cache.json")

cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
print(f"📂 Cache : {len(cache)} entrées")

with open(MASTER, encoding="utf-8") as f:
    master = json.load(f)

targets = [
    m for m in master
    if m.get("city") and m.get("end_date") and m.get("target_c") is not None
]
print(f"📂 {len(targets):,} marchés cibles\n")

results = []
done = errors = cached = 0

for i, m in enumerate(targets):
    city     = m["city"]
    end_date = m["end_date"][:10]
    cid      = m["condition_id"]
    try:
        month = datetime.strptime(end_date, "%Y-%m-%d").month
    except:
        continue

    key = f"{city.lower()}_{month}"
    if key in cache:
        stats = cache[key]
        cached += 1
    else:
        stats = get_climate_stats(city, month)
        cache[key] = stats
        done += 1
        if stats is None:
            errors += 1

    if not stats:
        continue

    results.append({
        "condition_id":  cid,
        "city":          city,
        "end_date":      end_date,
        "month":         month,
        "target_c":      m["target_c"],
        "clim_mean":     stats["mean"],
        "clim_std":      stats["std"],
        "clim_p10":      stats["p10"],
        "clim_p25":      stats["p25"],
        "clim_p50":      stats["p50"],
        "clim_p75":      stats["p75"],
        "clim_p90":      stats["p90"],
        "clim_min":      stats["min"],
        "clim_max":      stats["max"],
        "clim_n":        stats["n"],
        # Feature clé : écart target vs médiane climatologique
        "target_vs_clim_median": round(m["target_c"] - stats["p50"], 2),
        "target_vs_clim_mean":   round(m["target_c"] - stats["mean"], 2),
    })

    if (i + 1) % 100 == 0:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        print(f"  [{i+1:>5}/{len(targets)}] {(i+1)/len(targets)*100:.1f}% | fetch={done} cache={cached} err={errors}")

with open(CACHE, "w", encoding="utf-8") as f:
    json.dump(cache, f)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)

print(f"\n{'═'*55}")
print(f"  CLIMATOLOGIE — RÉSUMÉ")
print(f"{'═'*55}")
print(f"  Marchés traités     : {len(results):,}")
print(f"  Nouveaux fetches    : {done:,}")
print(f"  Depuis cache        : {cached:,}")
print(f"  Erreurs             : {errors:,}")
print(f"{'═'*55}")
print(f"\n💾 {OUT}")