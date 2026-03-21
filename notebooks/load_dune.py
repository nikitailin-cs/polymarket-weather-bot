"""
Récupère les données Polymarket météo depuis Dune Analytics.
Injecte dans master_weather.json via build_master.py
"""
import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv
load_dotenv()

from dune_client.client import DuneClient
from dune_client.query import QueryBase
from dune_client.types import QueryParameter

ROOT      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
OUT_DIR   = os.path.join(ROOT, "data", "raw", "dune")
OUT_FILE  = os.path.join(OUT_DIR, "weather_trades.json")
os.makedirs(OUT_DIR, exist_ok=True)

DUNE_API_KEY = os.getenv("DUNE_API_KEY", "")
if not DUNE_API_KEY:
    print("❌ DUNE_API_KEY manquante dans .env")
    sys.exit(1)

dune = DuneClient(api_key=DUNE_API_KEY)

# ─────────────────────────────────────────────
# REQUÊTE 1 — Prix historiques marchés résolus
# Query publique Polymarket : prix YES token à J-1, J-2, J-7 avant résolution
# ─────────────────────────────────────────────
print("📊 Dune — Query 1 : prix historiques marchés météo résolus...")
try:
    query = QueryBase(query_id=6875134)
    result = dune.run_query_dataframe(query)
    print(f"   ✅ {len(result):,} lignes reçues")
    print(f"   Colonnes : {list(result.columns)}")
    result.to_json(
        os.path.join(OUT_DIR, "query_4230507.json"),
        orient="records", indent=2, default_handler=str
    )
except Exception as e:
    print(f"   ⚠️ Query 4230507 : {e}")
    result = None

# ─────────────────────────────────────────────
# REQUÊTE 2 — Tous les trades Polymarket météo
# Requête SQL custom sur les tables Dune Polymarket
# ─────────────────────────────────────────────
print("\n📊 Dune — Query 2 : trades météo custom SQL...")

WEATHER_SQL = """
SELECT
    m.condition_id,
    m.question,
    t.timestamp,
    t.price,
    t.amount,
    t.side,
    t.outcome,
    m.end_time,
    m.resolved_price,
    m.volume_usd
FROM polymarket_polygon.markets m
JOIN polymarket_polygon.trades t
    ON m.condition_id = t.condition_id
WHERE
    m.closed = true
    AND (
        LOWER(m.question) LIKE '%temperature%'
        OR LOWER(m.question) LIKE '%celsius%'
        OR LOWER(m.question) LIKE '%fahrenheit%'
        OR LOWER(m.question) LIKE '%hurricane%'
        OR LOWER(m.question) LIKE '%precipitation%'
        OR LOWER(m.question) LIKE '%rainfall%'
        OR LOWER(m.question) LIKE '%snowfall%'
        OR LOWER(m.question) LIKE '%tornado%'
    )
    AND t.timestamp >= m.end_time - INTERVAL '3' DAY
    AND t.timestamp <= m.end_time
ORDER BY m.condition_id, t.timestamp
"""

try:
    from dune_client.models import ExecutionState
    exec_response = dune.execute_query(
        QueryBase(query_id=0),  # 0 = requête ad-hoc
        performance="medium"
    )
    print("   ℹ️  Requête ad-hoc non supportée sur plan gratuit")
    print("   → On utilise uniquement les queries publiques")
except Exception as e:
    print(f"   ℹ️  {e}")

# ─────────────────────────────────────────────
# REQUÊTE 3 — Queries publiques Polymarket utiles
# ─────────────────────────────────────────────
PUBLIC_QUERIES = {
    # Marchés Polymarket avec volumes et résolutions
    "polymarket_markets":   3237800,
    # Trades par marché
    "polymarket_trades":    3237801,
    # Prix historiques tokens YES
    "yes_token_prices":     4230507,
}

results = {}
for name, qid in PUBLIC_QUERIES.items():
    print(f"\n📊 Query {qid} ({name})...")
    try:
        df = dune.run_query_dataframe(QueryBase(query_id=qid))
        print(f"   ✅ {len(df):,} lignes | colonnes : {list(df.columns[:6])}")
        
        # Filtre météo si colonne question disponible
        if "question" in df.columns:
            wx_mask = df["question"].str.lower().str.contains(
                "temperature|celsius|fahrenheit|hurricane|precipitation|tornado",
                na=False
            )
            df_wx = df[wx_mask]
            print(f"   🌤️  {len(df_wx):,} lignes météo")
            df_wx.to_json(
                os.path.join(OUT_DIR, f"{name}_weather.json"),
                orient="records", indent=2, default_handler=str
            )
            results[name] = df_wx.to_dict("records")
        else:
            df.to_json(
                os.path.join(OUT_DIR, f"{name}.json"),
                orient="records", indent=2, default_handler=str
            )
            results[name] = df.to_dict("records")
    except Exception as e:
        print(f"   ⚠️ {e}")

# ─────────────────────────────────────────────
# SAUVEGARDE CONSOLIDÉE
# ─────────────────────────────────────────────
with open(OUT_FILE, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, default=str)

print(f"\n{'═'*55}")
print(f"  DUNE — RÉSUMÉ")
print(f"{'═'*55}")
for name, data in results.items():
    print(f"  {name:<30} : {len(data):,} enregistrements")
print(f"  Fichier : {OUT_FILE}")
print(f"{'═'*55}")
print(f"\n  Prochaine étape : python notebooks/build_master.py")
print(f"  (intégrera automatiquement les données Dune)")