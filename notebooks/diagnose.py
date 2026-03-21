import json, os
markets = json.load(open('data/raw/historical_markets.json'))
prices  = {p['condition_id']: p for p in json.load(open('data/raw/historical_prices.json'))}
resolved = [m for m in markets if m.get('resolved') and m.get('yes_won') is not None]
no_price = [m for m in resolved if not prices.get(m['condition_id'], {}).get('yes_price')]
print(f'Résolus total     : {len(resolved)}')
print(f'Avec prix CLOB    : {len(resolved)-len(no_price)}')
print(f'Sans prix         : {len(no_price)}\n')
for m in sorted(no_price, key=lambda x: x['end_date']):
    p   = prices.get(m['condition_id'], {})
    pts = p.get('data_points', '?')
    print(f"  {m['end_date']} | pts={pts:>3} | {m['city']:<18} | {m['question'][:55]}")