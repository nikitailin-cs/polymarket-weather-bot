"""
Récupère les résultats cachés des queries Dune sans re-exécuter.
"""
import os, sys, json, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv
load_dotenv()

API_KEY = os.getenv("DUNE_API_KEY", "")
DUNE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "raw", "dune")
os.makedirs(DUNE_DIR, exist_ok=True)

QUERIES = {
    6875261: "market_prices_hourly_2024h1.json",
    6876427: "market_prices_hourly_2024h2.json",
    6876429: "market_prices_hourly_2025_2026.json",
}

for QUERY_ID, filename in QUERIES.items():
    OUT = os.path.join(DUNE_DIR, filename)
    print(f"\n{'═'*50}")
    print(f"Query {QUERY_ID} → {filename}")
    print(f"{'═'*50}")

    all_rows = []
    offset   = 0
    limit    = 32000

    while True:
        url = f"https://api.dune.com/api/v1/query/{QUERY_ID}/results?limit={limit}&offset={offset}"
        req = urllib.request.Request(url, headers={
            "X-Dune-API-Key": API_KEY,
            "User-Agent": "Mozilla/5.0"
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
            rows  = data.get("result", {}).get("rows", [])
            total = data.get("result", {}).get("metadata", {}).get("total_row_count", 0)
            all_rows.extend(rows)
            print(f"  {len(all_rows):,} / {total:,} lignes...", end="\r")
            if len(rows) == 0 or len(all_rows) >= total:
                break
            offset += len(rows)
        except Exception as e:
            print(f"\n❌ offset={offset}: {e}")
            break

    print(f"\n✅ {len(all_rows):,} lignes")
    if all_rows:
        print(f"Colonnes : {list(all_rows[0].keys())}")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, indent=2)
    print(f"💾 {OUT}")