import json, os

master = json.load(open('data/raw/master_weather.json', encoding='utf-8'))
resolved = [m for m in master if m.get('resolved') is True]
print(f"Résolus : {len(resolved)}")

has_token = [m for m in resolved if m.get('yes_token_id')]
no_token  = [m for m in resolved if not m.get('yes_token_id')]
print(f"Avec yes_token_id : {len(has_token)}")
print(f"Sans yes_token_id : {len(no_token)}")

# Montre un exemple avec et sans token
if has_token:
    m = has_token[0]
    print(f"\nAvec token : {m['condition_id'][:20]} | token={str(m['yes_token_id'])[:30]}")
if no_token:
    m = no_token[0]
    print(f"Sans token : {m['condition_id'][:20]} | raw_keys={list((m.get('_raw_market') or {}).keys())[:5] if '_raw_market' in m else 'pas de _raw_market'}")