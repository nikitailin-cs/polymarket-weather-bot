"""
Goldsky Polymarket — utilise les vraies entités découvertes.
"""
import os, sys, json, urllib.request, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

ROOT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MASTER  = os.path.join(ROOT, "data", "raw", "master_weather.json")
OUT_DIR = os.path.join(ROOT, "data", "raw", "goldsky")
os.makedirs(OUT_DIR, exist_ok=True)

OB_URL = "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/orderbook-subgraph/0.0.1/gn"

def gql(query, variables=None):
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(OB_URL, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

# ── Étape 1 : explore les champs de chaque entité ────────
print("🔍 Exploration des champs...\n")

for entity, sample_query in [
    ("orderFilledEvents", """{ orderFilledEvents(first: 2) {
        id timestamp price size side maker taker
        makerAssetId takerAssetId fee
    }}"""),
    ("ordersMatchedEvents", """{ ordersMatchedEvents(first: 2) {
        id timestamp makerAmountFilled takerAmountFilled
        makerAssetId takerAssetId
    }}"""),
    ("orderbooks", """{ orderbooks(first: 2) {
        id marketId lastTradePrice
        bidSide { price size }
        askSide { price size }
    }}"""),
    ("marketDatas", """{ marketDatas(first: 2) {
        id condition { id }
        lastActiveDay volume
    }}"""),
]:
    try:
        r = gql(sample_query)
        data = list(r.get("data", {}).values())[0]
        if data:
            print(f"✅ {entity}")
            print(f"   Champs : {list(data[0].keys())}")
            print(f"   Ex : {json.dumps(data[0], default=str)[:200]}\n")
        else:
            print(f"⚠️  {entity} — vide\n")
    except Exception as e:
        # Essai sans champs imbriqués
        try:
            simple = f"{{ {entity}(first: 2) {{ id }} }}"
            r2 = gql(simple)
            data2 = list(r2.get("data", {}).values())[0]
            errors = r2.get("errors", [])
            if errors:
                print(f"❌ {entity} — {errors[0].get('message','')[:100]}\n")
            else:
                print(f"⚠️  {entity} — existe mais champs différents\n")
        except Exception as e2:
            print(f"❌ {entity} — {e2}\n")

# ── Étape 2 : charge les condition_ids météo ─────────────
print("📂 Chargement master...")
with open(MASTER, encoding="utf-8") as f:
    master = json.load(f)
wx_cids = {m["condition_id"] for m in master}
print(f"   {len(wx_cids):,} condition_ids météo\n")

# ── Étape 3 : récupère orderFilledEvents paginé ──────────
print("📊 Récupération orderFilledEvents...")
all_trades = []
skip = 0

while skip < 200000:
    try:
        resp = gql("""
        query($skip: Int!, $first: Int!) {
          orderFilledEvents(
            first: $first, skip: $skip,
            orderBy: timestamp, orderDirection: desc
          ) {
            id timestamp price size side
            makerAssetId takerAssetId
            maker taker fee
          }
        }
        """, {"skip": skip, "first": 1000})
        
        errors = resp.get("errors")
        if errors:
            print(f"   GraphQL error: {errors[0]['message']}")
            break
            
        batch = resp.get("data", {}).get("orderFilledEvents", [])
        if not batch:
            break
        all_trades.extend(batch)
        sys.stdout.write(f"\r   {len(all_trades):,} trades récupérés...")
        sys.stdout.flush()
        skip += 1000
        time.sleep(0.2)
    except Exception as e:
        print(f"\n   ⚠️ {e}")
        time.sleep(2)
        break

print(f"\n   Total : {len(all_trades):,} trades\n")

# ── Étape 4 : filtre sur marchés météo ───────────────────
print("🔍 Filtrage sur marchés météo...")
# Les trades ont makerAssetId/takerAssetId = token_id
# On construit un set des token_ids météo depuis le master
wx_token_ids = set()
for m in master:
    if m.get("yes_token_id"):
        wx_token_ids.add(str(m["yes_token_id"]))
    if m.get("no_token_id"):
        wx_token_ids.add(str(m["no_token_id"]))

wx_trades = [
    t for t in all_trades
    if t.get("makerAssetId") in wx_token_ids
    or t.get("takerAssetId") in wx_token_ids
]
print(f"   {len(wx_trades):,} trades météo / {len(all_trades):,} total\n")

# ── Étape 5 : récupère marketDatas pour les marchés actifs
print("📊 Récupération marketDatas...")
market_datas = []
skip = 0
while skip < 10000:
    try:
        resp = gql("""
        query($skip: Int!, $first: Int!) {
          marketDatas(first: $first, skip: $skip) {
            id
            condition { id }
            lastActiveDay
            volume
          }
        }
        """, {"skip": skip, "first": 1000})
        batch = resp.get("data", {}).get("marketDatas", [])
        if not batch:
            break
        # Filtre météo
        wx_batch = [
            m for m in batch
            if (m.get("condition") or {}).get("id") in wx_cids
        ]
        market_datas.extend(wx_batch)
        sys.stdout.write(f"\r   {skip+len(batch):,} scannés | {len(market_datas)} météo...")
        sys.stdout.flush()
        skip += 1000
        time.sleep(0.2)
    except Exception as e:
        print(f"\n   ⚠️ {e}")
        break

print(f"\n   {len(market_datas):,} marchés météo dans marketDatas\n")

# ── Sauvegarde ───────────────────────────────────────────
result = {"trades": wx_trades, "market_datas": market_datas}
out = os.path.join(OUT_DIR, "goldsky_data.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, default=str)

print(f"{'═'*55}")
print(f"  GOLDSKY — RÉSUMÉ")
print(f"{'═'*55}")
print(f"  Trades météo          : {len(wx_trades):,}")
print(f"  MarketDatas météo     : {len(market_datas):,}")
print(f"  Fichier               : {out}")
print(f"{'═'*55}")