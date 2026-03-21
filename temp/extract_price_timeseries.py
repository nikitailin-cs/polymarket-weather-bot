"""
Calcule prix horaires et journaliers depuis quant.parquet via DuckDB.
Remplace complètement Dune market_prices_hourly/daily.
"""
import os, sys, json, duckdb

ROOT   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
QUANT  = "S:/POLYMARKET/data/raw/hf/quant.parquet"
MASTER = os.path.join(ROOT, "data", "raw", "master_weather.json")
OUT_H  = os.path.join(ROOT, "data", "raw", "dune", "market_prices_hourly_local.json")
OUT_D  = os.path.join(ROOT, "data", "raw", "dune", "market_prices_daily_local.json")

print("📂 Chargement master...")
with open(MASTER, encoding="utf-8") as f:
    master = json.load(f)

resolved = [m for m in master if m.get("resolved") is True and m.get("condition_id")]
cid_list = ",".join(f"'{m['condition_id']}'" for m in resolved)
print(f"   {len(resolved):,} marchés résolus\n")

con = duckdb.connect()

# ── Prix journaliers ─────────────────────────────────────
print("📊 Calcul prix journaliers...")
daily = con.execute(f"""
    SELECT
        condition_id,
        DATE_TRUNC('day', TO_TIMESTAMP(timestamp)) as day,
        AVG(price)                                  as price_avg,
        MIN(price)                                  as price_min,
        MAX(price)                                  as price_max,
        FIRST(price ORDER BY timestamp)             as price_open,
        LAST(price ORDER BY timestamp)              as price_close,
        COUNT(*)                                    as trade_count,
        SUM(usd_amount)                             as volume_usd
    FROM read_parquet('{QUANT}')
    WHERE condition_id IN ({cid_list})
    GROUP BY condition_id, DATE_TRUNC('day', TO_TIMESTAMP(timestamp))
    ORDER BY condition_id, day
""").fetchdf()

print(f"   ✅ {len(daily):,} lignes journalières")
daily.to_json(OUT_D, orient="records", indent=2, default_handler=str)
print(f"💾 {OUT_D}\n")

# ── Prix horaires (fenêtres 6h relatives à l'expiry) ─────
print("📊 Calcul prix horaires par fenêtre 6h...")

# Construit table des dates d'expiry
market_values = ",".join(
    f"('{m['condition_id']}', '{m['end_date']}')"
    for m in resolved if m.get("end_date")
)

hourly = con.execute(f"""
    WITH market_dates AS (
        SELECT condition_id, end_date,
            EPOCH(CAST(end_date AS TIMESTAMP)) as expiry_ts
        FROM (VALUES {market_values}) t(condition_id, end_date)
    ),
    trades_with_expiry AS (
        SELECT
            q.condition_id,
            q.price,
            q.usd_amount,
            q.timestamp,
            md.expiry_ts,
            -- Fenêtre 6h relative à l'expiry
            CAST((md.expiry_ts - q.timestamp) / (6*3600) AS INTEGER) * 6 as hours_before_expiry
        FROM read_parquet('{QUANT}') q
        JOIN market_dates md ON q.condition_id = md.condition_id
        WHERE q.timestamp <= md.expiry_ts
          AND q.timestamp >= md.expiry_ts - 30*24*3600
    )
    SELECT
        condition_id,
        hours_before_expiry,
        AVG(price)                            as price_avg,
        MIN(price)                            as price_min,
        MAX(price)                            as price_max,
        FIRST(price ORDER BY timestamp DESC)  as price_close,
        COUNT(*)                              as trade_count,
        SUM(usd_amount)                       as volume_usd
    FROM trades_with_expiry
    GROUP BY condition_id, hours_before_expiry
    ORDER BY condition_id, hours_before_expiry
""").fetchdf()

print(f"   ✅ {len(hourly):,} lignes horaires")
hourly.to_json(OUT_H, orient="records", indent=2, default_handler=str)
print(f"💾 {OUT_H}")

print(f"\n✅ Terminé — pas besoin de Dune !")