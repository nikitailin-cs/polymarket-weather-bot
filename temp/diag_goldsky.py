import urllib.request, json

URL = "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/polymarket-orderbook-resync/prod/gn"

def gql(query):
    payload = json.dumps({"query": query}).encode()
    req = urllib.request.Request(URL, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

# Test 1 — enrichedOrderFilleds sans filtre
print("=== enrichedOrderFilleds ===")
r = gql("""{ enrichedOrderFilleds(first: 2) { id timestamp } }""")
print(json.dumps(r, indent=2))

# Test 2 — conditions sans filtre
print("\n=== conditions ===")
r = gql("""{ conditions(first: 2) { id resolved resolutionTimestamp } }""")
print(json.dumps(r, indent=2))

# Test 3 — marketDatas
print("\n=== marketDatas ===")
r = gql("""{ marketDatas(first: 2) { id condition { id } } }""")
print(json.dumps(r, indent=2))