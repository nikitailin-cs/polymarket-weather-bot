"""
Extraction exhaustive de TOUS les marchés météo depuis le dataset HuggingFace.
Étape 1 : markets.parquet → liste condition_ids météo
Étape 2 : stream quant.parquet → prix réels pour ces marchés
Output  : data/raw/hf/weather_complete.json (prêt pour backtest + training IA)
"""
import os, sys, json, re
import pandas as pd
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


ROOT     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HF_DIR   = os.path.join(ROOT, "data", "raw", "hf")
OUT_FILE = f"{HF_DIR}/weather_complete.json"
os.makedirs(HF_DIR, exist_ok=True)

# ─────────────────────────────────────────────
# KEYWORDS — large pour ne rien manquer
# ─────────────────────────────────────────────
KEYWORDS_STRONG = [
    "temperature", "celsius", "fahrenheit", "°c", "°f",
    "precipitation", "rainfall", "snowfall", "blizzard",
    "hurricane", "typhoon", "cyclone", "tornado", "tropical storm",
    "heatwave", "heat wave", "cold wave", "frost", "freeze",
    "windspeed", "wind speed", "humidity",
    "named storm", "atlantic hurricane", "noaa",
]

KEYWORDS_WEAK = [
    "rain", "snow", "hail", "sleet", "flood", "drought",
    "storm", "thunderstorm", "wind", "winds",
    "weather", "climate", "forecast", "meteorolog",
    "inches of", "mm of", "cm of",
    "highest temp", "lowest temp", "max temp",
]

# Patterns structurels pour valider les keywords faibles
PATTERNS = [
    r"highest temperature in \w+",
    r"lowest temperature in \w+",
    r"temperature in .{2,30} be \d+",
    r"\d+\s*°\s*[cfCF]",
    r"\d+\s*(mm|cm|inches?) of (rain|snow|precip)",
    r"make landfall",
    r"hurricane season",
    r"named storm",
    r"(mph|km/h|knots)",
]

def is_weather(question: str) -> bool:
    q = (question or "").lower()
    # Blacklist
    for bl in ["winds of winter", "trade winds", "second wind",
               "wind energy", "wind farm", "wind power",
               "storm trooper", "brainstorm", "firestorm"]:
        if bl in q:
            return False
    # Strong keyword → accept direct
    for kw in KEYWORDS_STRONG:
        if kw in q:
            return True
    # Weak keyword → need structural pattern
    for kw in KEYWORDS_WEAK:
        if re.search(r"\b" + re.escape(kw) + r"\b", q):
            for p in PATTERNS:
                if re.search(p, q):
                    return True
    return False

# ─────────────────────────────────────────────
# ÉTAPE 1 — Extraire marchés météo de markets.parquet
# ─────────────────────────────────────────────
MARKETS_FILE = f"{HF_DIR}/markets.parquet"

if not os.path.exists(MARKETS_FILE):
    print("❌ markets.parquet manquant !")
    print(f"   Télécharge : https://huggingface.co/datasets/SII-WANGZJ/Polymarket_data/resolve/main/markets.parquet")
    print(f"   Sauvegarde dans : {os.path.abspath(MARKETS_FILE)}")
    sys.exit(1)

print("📂 Chargement markets.parquet...")
df = pd.read_parquet(MARKETS_FILE)
print(f"   {len(df):,} marchés totaux")
print(f"   Colonnes : {list(df.columns)}\n")

# Filtre météo
# Vectorisé — 100x plus rapide que apply
q_series = df["question"].fillna("").str.lower()

# Strong keywords — direct
mask_strong = q_series.str.contains(
    "|".join(KEYWORDS_STRONG), regex=True, na=False
)

# Weak keywords avec pattern structurel
mask_weak = q_series.str.contains(
    "|".join(KEYWORDS_WEAK), regex=True, na=False
)
mask_pattern = q_series.str.contains(
    "|".join(PATTERNS), regex=True, na=False
)

# Blacklist
mask_black = q_series.str.contains(
    "winds of winter|trade winds|second wind|wind energy|wind farm|wind power|storm trooper|brainstorm|firestorm",
    regex=True, na=False
)

mask = (mask_strong | (mask_weak & mask_pattern)) & ~mask_black
weather_df = df[mask].copy()
print(f"✅ {len(weather_df):,} marchés météo trouvés\n")

# Affiche distribution
print("📊 Distribution par type :")
for kw in ["temperature", "hurricane", "rain", "snow", "flood", "wind", "precipitation"]:
    n = weather_df.apply(
        lambda r: kw in str(r.get("question","")).lower(), axis=1
    ).sum()
    if n > 0:
        print(f"   {kw:<20} {n:>4}")

print(f"\n📅 Période couverte :")
for col in ["end_date_iso", "end_date", "endDate", "created_at"]:
    if col in weather_df.columns:
        print(f"   {col}: {weather_df[col].min()} → {weather_df[col].max()}")
        break

print(f"\n🔑 Colonnes résolution disponibles :")
for col in weather_df.columns:
    if any(x in col.lower() for x in ["resolv", "winner", "outcome", "result", "closed", "answer"]):
        print(f"   {col}: {weather_df[col].value_counts().head(3).to_dict()}")

# Sauvegarde liste des marchés météo
condition_ids = set()
markets_out = []
for _, row in weather_df.iterrows():
    rec = row.to_dict()
    # Normalise condition_id
    cid = str(rec.get("condition_id") or rec.get("conditionId") or rec.get("id") or "")
    if cid:
        condition_ids.add(cid)
    rec["_cid_normalized"] = cid
    markets_out.append(rec)

print(f"\n💾 {len(markets_out)} marchés météo | {len(condition_ids)} condition_ids uniques")

# Sauvegarde intermédiaire
with open(f"{HF_DIR}/weather_markets_only.json", "w", encoding="utf-8") as f:
    json.dump(markets_out, f, indent=2, default=str)
print(f"   → {HF_DIR}/weather_markets_only.json\n")

# ─────────────────────────────────────────────
# ÉTAPE 2 — Récupère prix réels depuis quant.parquet (streaming)
# ─────────────────────────────────────────────
QUANT_FILE = f"{HF_DIR}/quant.parquet"

if not os.path.exists(QUANT_FILE):
    print("⚠️  quant.parquet absent — on skip les prix de trading")
    print(f"   Pour l'avoir : télécharge https://huggingface.co/datasets/SII-WANGZJ/Polymarket_data/resolve/main/quant.parquet")
    print(f"   (21GB — optionnel, mais donne les vrais prix pre-résolution)\n")
else:
    print(f"📂 Lecture quant.parquet en streaming (filtre sur {len(condition_ids)} marchés météo)...")
    # Lecture par chunks pour éviter de charger 21GB en RAM
    prices_by_cid = {}
    chunk_size = 100_000
    total_read = 0

    for chunk in pd.read_parquet(QUANT_FILE, chunksize=chunk_size):
        total_read += len(chunk)
        # Filtre sur nos marchés météo
        cid_col = next((c for c in ["condition_id","conditionId"] if c in chunk.columns), None)
        if cid_col:
            sub = chunk[chunk[cid_col].astype(str).isin(condition_ids)]
            for cid, grp in sub.groupby(cid_col):
                if cid not in prices_by_cid:
                    prices_by_cid[cid] = []
                prices_by_cid[cid].extend(grp.to_dict("records"))

        if total_read % 1_000_000 == 0:
            print(f"   {total_read:,} lignes lues | {len(prices_by_cid)} marchés avec prix")

    print(f"   ✅ {len(prices_by_cid)} marchés météo avec historique de trades\n")

# ─────────────────────────────────────────────
# MERGE FINAL — un enregistrement par marché
# ─────────────────────────────────────────────
print("🔗 Merge marchés + prix...")
final = []
for m in markets_out:
    cid = m.get("_cid_normalized", "")
    record = {
        # Identifiants
        "condition_id":  cid,
        "question":      m.get("question") or m.get("title") or "",
        # Dates
        "end_date":      str(m.get("end_date_iso") or m.get("end_date") or m.get("endDate") or ""),
        "created_at":    str(m.get("created_at") or ""),
        # Résultat
        "resolved":      bool(m.get("resolved") or m.get("closed")),
        "yes_won":       m.get("winner") or m.get("outcome") or m.get("result"),
        "final_yes_price": m.get("outcomePrices") or m.get("final_price"),
        # Volume
        "volume":        float(m.get("volume") or 0),
        # Données brutes complètes (pour training IA plus tard)
        "_raw_market":   m,
        # Prix de trading pre-résolution (si quant.parquet dispo)
        "_trades":       prices_by_cid.get(cid, []) if 'prices_by_cid' in dir() else [],
    }
    final.append(record)

final.sort(key=lambda x: x["end_date"])

with open(OUT_FILE, "w", encoding="utf-8") as f:
    json.dump(final, f, indent=2, default=str)

print(f"\n{'═'*60}")
print(f"  EXTRACTION COMPLÈTE")
print(f"{'═'*60}")
print(f"  Marchés météo totaux      : {len(final)}")
print(f"  Résolus                   : {sum(1 for m in final if m['resolved'])}")
print(f"  Avec trades (prix réels)  : {sum(1 for m in final if m['_trades'])}")
print(f"  Volume total              : ${sum(m['volume'] for m in final):,.0f}")
print(f"  Fichier final             : {OUT_FILE}")
print(f"{'═'*60}")
print(f"\n  Prochaine étape : python notebooks/backtest.py")
print(f"  (utilisera automatiquement weather_complete.json)")