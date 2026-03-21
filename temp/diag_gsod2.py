import json

cache = json.load(open('data/raw/noaa_gsod_cache.json', encoding='utf-8'))
with_tmax = [(k,v) for k,v in cache.items() if v.get('gsod_tmax') is not None]
without   = [(k,v) for k,v in cache.items() if v.get('gsod_tmax') is None]

print(f"Avec tmax    : {len(with_tmax)}")
print(f"Sans tmax    : {len(without)}")
print(f"\nExemple AVEC : {with_tmax[0] if with_tmax else 'none'}")
print(f"Exemple SANS : {without[0] if without else 'none'}")

print("\n=== CACHE STATS ===")
cache = json.load(open('data/raw/noaa_gsod_cache.json', encoding='utf-8'))
with_tmax = [(k,v) for k,v in cache.items() if v.get('gsod_tmax') is not None]
without   = [(k,v) for k,v in cache.items() if v.get('gsod_tmax') is None]
print(f"Avec tmax    : {len(with_tmax)}")
print(f"Sans tmax    : {len(without)}")
print(f"\nExemple AVEC : {with_tmax[0] if with_tmax else 'none'}")
print(f"Exemple SANS : {without[0] if without else 'none'}")