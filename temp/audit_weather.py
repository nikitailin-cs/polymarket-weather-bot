"""
Audit exhaustif de l'extraction météo depuis markets.parquet.
Vérifie qu'on n'a rien manqué.
"""
import pandas as pd, json, re, os

ROOT     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MARKETS  = os.path.join(ROOT, "data", "raw", "hf", "markets.parquet")
WX_FILE  = os.path.join(ROOT, "data", "raw", "hf", "weather_complete.json")

df = pd.read_parquet(MARKETS)
print(f"Total marchés : {len(df):,}\n")

# Charge les condition_ids déjà extraits
with open(WX_FILE, encoding="utf-8") as f:
    extracted = json.load(f)
extracted_cids = {m["condition_id"] for m in extracted}
print(f"Déjà extraits : {len(extracted_cids):,}\n")

q = df["question"].fillna("").str.lower()

# ── Test 1 : mots-clés météo directs ──────────────────────
DIRECT = [
    "temperature", "celsius", "fahrenheit", "°c", "°f",
    "precipitation", "rainfall", "snowfall", "hurricane",
    "typhoon", "cyclone", "tornado", "blizzard", "heatwave",
    "heat wave", "windspeed", "humidity", "named storm",
    "atlantic hurricane", "noaa", "highest temp", "lowest temp",
    "inches of rain", "inches of snow", "mm of rain",
    "weather forecast", "meteorolog",
]
mask_direct = q.str.contains("|".join(DIRECT), regex=True, na=False)
direct_df = df[mask_direct]

# ── Test 2 : patterns structurels météo ──────────────────
PATTERNS = [
    r"will it (rain|snow|flood)",
    r"(rain|snow|storm|flood|drought).{0,30}(day|week|month|season)",
    r"\d+\s*(mm|cm|inches?)\s+of\s+(rain|snow|precip)",
    r"\d+\s*°\s*[cfCF]",
    r"(make|hit)\s+landfall",
    r"wind\s+speed\s+\d+",
    r"above\s+\d+\s*(degrees|°)",
    r"below\s+\d+\s*(degrees|°)",
    r"category\s+[1-5]\s+hurricane",
    r"tropical\s+(storm|depression|cyclone)",
    r"(el\s+ni[ñn]o|la\s+ni[ñn]a)",
    r"sea\s+surface\s+temperature",
    r"arctic\s+(blast|air|vortex)",
]
mask_pattern = q.str.contains("|".join(PATTERNS), regex=True, na=False)
pattern_df = df[mask_pattern]

# ── Test 3 : event_title météo ────────────────────────────
WEATHER_EVENTS = [
    "weather", "climate", "temperature", "hurricane", "storm",
    "rain", "snow", "flood", "drought", "wind", "forecast",
    "meteorolog", "precipitation",
]
et = df["event_title"].fillna("").str.lower()
mask_event = et.str.contains("|".join(WEATHER_EVENTS), regex=True, na=False)
event_df = df[mask_event]

# ── Union de tout ─────────────────────────────────────────
all_weather_cids = (
    set(direct_df["condition_id"]) |
    set(pattern_df["condition_id"]) |
    set(event_df["condition_id"])
)
all_weather = df[df["condition_id"].isin(all_weather_cids)]

# ── Comparaison ───────────────────────────────────────────
missed_cids = all_weather_cids - extracted_cids
extra_cids  = extracted_cids - all_weather_cids
missed_df   = df[df["condition_id"].isin(missed_cids)]

print(f"{'═'*65}")
print(f"  AUDIT EXTRACTION MÉTÉO")
print(f"{'═'*65}")
print(f"  Direct keywords       : {len(direct_df):,}")
print(f"  Structural patterns   : {len(pattern_df):,}")
print(f"  Event title           : {len(event_df):,}")
print(f"  ─────────────────────────────")
print(f"  UNION totale          : {len(all_weather):,}")
print(f"  Déjà extraits         : {len(extracted_cids):,}")
print(f"  ✅ Correctement extraits : {len(extracted_cids & all_weather_cids):,}")
print(f"  ❌ MANQUÉS             : {len(missed_cids):,}")
print(f"  ⚠️  Faux positifs extra : {len(extra_cids):,}")
print(f"{'═'*65}")

if missed_df is not None and len(missed_df) > 0:
    print(f"\n❌ Exemples manqués :")
    for _, row in missed_df.head(30).iterrows():
        print(f"  [{row['event_title'][:25]:<25}] {row['question'][:70]}")

    # Sauvegarde les manqués
    missed_out = os.path.join(ROOT, "data", "raw", "hf", "weather_missed.json")
    missed_df.to_json(missed_out, orient="records", indent=2, default_handler=str)
    print(f"\n💾 Manqués sauvegardés : {missed_out}")
else:
    print(f"\n✅ Aucun marché météo manqué — extraction exhaustive confirmée !")

# ── Analyse des event_titles météo pour catégoriser ──────
print(f"\n📊 Top event_titles météo :")
top_events = (
    all_weather["event_title"]
    .value_counts()
    .head(20)
)
for title, count in top_events.items():
    already = "✅" if df[df["event_title"]==title]["condition_id"].iloc[0] in extracted_cids else "❌"
    print(f"  {already} {count:>5}x  {title[:60]}")