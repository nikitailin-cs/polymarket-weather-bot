import json

cache = json.load(open('data/raw/noaa_gsod_cache.json', encoding='utf-8'))
print(f"Cache : {len(cache)} entrées")

# Montre les 5 premières
for k, v in list(cache.items())[:5]:
    print(f"\n{k}")
    print(f"  {v}")

# Compte les non-None
with_tmax = sum(1 for v in cache.values() if v.get("gsod_tmax") is not None)
print(f"\nAvec gsod_tmax non-null : {with_tmax}")