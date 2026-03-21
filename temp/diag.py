import json

hf = json.load(open('data/raw/hf/weather_complete.json', encoding='utf-8'))
m = hf[0]
print('=== PREMIER ENREGISTREMENT ===')
print('Champs top-level:', list(m.keys()))
print('resolved:', m.get('resolved'))
print('yes_won:', m.get('yes_won'))
print('final_yes_price:', m.get('final_yes_price'))
raw = m.get('_raw_market', {})
print()
print('=== _raw_market ===')
for k, v in raw.items():
    print(f'  {k}: {str(v)[:80]}')
print()
closed   = sum(1 for x in hf if x.get('_raw_market', {}).get('closed') == 1)
resolved = sum(1 for x in hf if x.get('resolved'))
yes_won  = sum(1 for x in hf if x.get('yes_won') is not None)
print(f'closed==1 dans _raw_market : {closed}')
print(f'resolved=True top level    : {resolved}')
print(f'yes_won non-null           : {yes_won}')