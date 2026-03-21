import urllib.request, json

URL = "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/orderbook-subgraph/0.0.1/gn"

def gql(query):
    payload = json.dumps({"query": query}).encode()
    req = urllib.request.Request(URL, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

# Introspection complète des 4 entités
for entity in ["OrderFilledEvent", "OrdersMatchedEvent", "Orderbook", "MarketData"]:
    print(f"\n=== {entity} ===")
    r = gql(f"""{{
        __type(name: "{entity}") {{
            fields {{ name type {{ name kind ofType {{ name }} }} }}
        }}
    }}""")
    fields = (r.get("data", {}).get("__type") or {}).get("fields", [])
    for f in fields:
        t = f["type"]
        type_name = t.get("name") or (t.get("ofType") or {}).get("name")
        print(f"  {f['name']}: {type_name}")