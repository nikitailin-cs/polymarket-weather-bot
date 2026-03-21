"""
Extraction exhaustive de TOUS les trades météo depuis quant.parquet via DuckDB.
Stratégie multi-passes pour ne rien manquer.
"""
import os, sys, json, duckdb

ROOT   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
QUANT  = "S:/POLYMARKET/data/raw/hf/quant.parquet"
MASTER = os.path.join(ROOT, "data", "raw", "master_weather.json")
OUT    = os.path.join(ROOT, "data", "raw", "hf", "quant_weather_prices.json")

print("📂 Chargement master...")
with open(MASTER, encoding="utf-8") as f:
    master = json.load(f)

resolved = [
    m for m in master
    if m.get("resolved") is True
    and m.get("end_date")
    and m.get("condition_id")
]
print(f"   {len(resolved):,} marchés résolus\n")

con = duckdb.connect()

# ── Diagnostic format condition_id ──────────────────────
print("🔍 Diagnostic format condition_ids...")
sample_q = con.execute(f"""
    SELECT DISTINCT condition_id FROM read_parquet('{QUANT}') LIMIT 3
""").fetchdf()
fmt_quant  = sample_q["condition_id"].tolist()[0]
fmt_master = resolved[0]["condition_id"]
print(f"   quant.parquet : {fmt_quant}")
print(f"   master        : {fmt_master}")
# Normalise si besoin
needs_lower = fmt_quant != fmt_quant.lower()
needs_0x    = fmt_quant.startswith("0x") != fmt_master.startswith("0x")
print(f"   Normalisation needed: lower={needs_lower} 0x={needs_0x}\n")

# Construit la table des marchés cibles
market_values = ",".join(
    f"('{m['condition_id']}', '{m['end_date']}')"
    for m in resolved
)

def run_query(window_h_start, window_h_end, label):
    print(f"📊 Passe {label} — fenêtre [{window_h_end}h → {window_h_start}h avant expiry]...")
    df = con.execute(f"""
        WITH market_dates AS (
            SELECT condition_id, end_date,
                EPOCH(CAST(end_date AS TIMESTAMP)) as expiry_ts
            FROM (VALUES {market_values}) t(condition_id, end_date)
        ),
        trades_window AS (
            SELECT
                q.condition_id,
                q.price,
                q.usd_amount,
                q.token_amount,
                q.side,
                q.timestamp,
                md.end_date,
                md.expiry_ts
            FROM read_parquet('{QUANT}') q
            JOIN market_dates md ON q.condition_id = md.condition_id
            WHERE q.timestamp >= md.expiry_ts - {window_h_start}*3600
              AND q.timestamp <= md.expiry_ts - {window_h_end}*3600
        )
        SELECT
            condition_id,
            end_date,
            -- Prix moyen pondéré par volume (le plus fiable)
            SUM(price * usd_amount) / NULLIF(SUM(usd_amount), 0)  AS yes_price_vwap,
            -- Médiane
            MEDIAN(price)                                           AS yes_price_median,
            -- Dernier prix avant expiry
            LAST(price ORDER BY timestamp)                          AS yes_price_last,
            -- Premier prix de la fenêtre
            FIRST(price ORDER BY timestamp)                         AS yes_price_first,
            COUNT(*)                                                AS trade_count,
            SUM(usd_amount)                                         AS volume_window,
            SUM(token_amount)                                       AS token_volume,
            -- Séparation BUY/SELL
            SUM(CASE WHEN side='BUY'  THEN usd_amount ELSE 0 END)  AS buy_volume,
            SUM(CASE WHEN side='SELL' THEN usd_amount ELSE 0 END)  AS sell_volume,
            -- Fenêtre utilisée (pour traçabilité)
            {window_h_start}                                        AS window_h_start,
            {window_h_end}                                          AS window_h_end
        FROM trades_window
        GROUP BY condition_id, end_date
        HAVING COUNT(*) >= 1
        ORDER BY end_date
    """).fetchdf()
    print(f"   ✅ {len(df):,} marchés trouvés")
    return df.to_dict("records")

# ── Passe 1 : fenêtre optimale 12h-72h ──────────────────
results_p1 = run_query(72, 12, "1 (12h-72h avant expiry)")
found_cids = {p["condition_id"] for p in results_p1}

# ── Passe 2 : fenêtre élargie 0h-168h pour les manqués ──
missing = [m for m in resolved if m["condition_id"] not in found_cids]
print(f"   {len(missing):,} marchés sans prix → passe 2")
if missing:
    market_values = ",".join(
        f"('{m['condition_id']}', '{m['end_date']}')"
        for m in missing
    )
    results_p2 = run_query(168, 0, "2 (0h-7j avant expiry)")
    found_cids.update(p["condition_id"] for p in results_p2)
else:
    results_p2 = []

# ── Passe 3 : fenêtre très large 0h-30j pour les encore manqués ──
market_values = ",".join(
    f"('{m['condition_id']}', '{m['end_date']}')"
    for m in resolved if m["condition_id"] not in found_cids
)
still_missing = [m for m in resolved if m["condition_id"] not in found_cids]
print(f"   {len(still_missing):,} marchés sans prix → passe 3")
if still_missing:
    market_values = ",".join(
        f"('{m['condition_id']}', '{m['end_date']}')"
        for m in still_missing
    )
    results_p3 = run_query(720, 0, "3 (0h-30j avant expiry)")
else:
    results_p3 = []

# ── Merge final — priorité passe 1 > 2 > 3 ──────────────
all_results = {}
for p in results_p3:
    all_results[p["condition_id"]] = p
for p in results_p2:
    all_results[p["condition_id"]] = p
for p in results_p1:
    all_results[p["condition_id"]] = p

prices_out = list(all_results.values())
prices_out.sort(key=lambda x: x["end_date"])

# ── Stats finales ────────────────────────────────────────
total_no_price = len([m for m in resolved if m["condition_id"] not in all_results])

print(f"\n{'═'*60}")
print(f"  QUANT.PARQUET — EXTRACTION COMPLÈTE")
print(f"{'═'*60}")
print(f"  Marchés résolus total     : {len(resolved):,}")
print(f"  Passe 1 (12h-72h)         : {len(results_p1):,}")
print(f"  Passe 2 (0h-7j)           : {len(results_p2):,}")
print(f"  Passe 3 (0h-30j)          : {len(results_p3):,}")
print(f"  ─────────────────────────────────────")
print(f"  TOTAL avec prix réel      : {len(prices_out):,}")
print(f"  Sans aucun trade          : {total_no_price:,}  (marchés sans liquidité)")
print(f"  Couverture                : {len(prices_out)/len(resolved)*100:.1f}%")
if prices_out:
    avg = sum(float(p["trade_count"]) for p in prices_out) / len(prices_out)
    print(f"  Trades/marché moyenne     : {avg:.1f}")
print(f"{'═'*60}")

# Sauvegarde
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(prices_out, f, indent=2, default=str)
print(f"\n💾 {OUT}")
print(f"   → python notebooks/build_master.py")