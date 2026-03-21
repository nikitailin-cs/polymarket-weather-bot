"""
Pipeline d'agrégation multi-sources.
Chaque source enrichit le master sans jamais écraser les données existantes.
"""
import os, sys, json, re
from datetime import datetime, timezone

ROOT       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA_RAW   = os.path.join(ROOT, "data", "raw")
MASTER     = os.path.join(DATA_RAW, "master_weather.json")
HF_FILE    = os.path.join(DATA_RAW, "hf", "weather_complete.json")
HIST_FILE  = os.path.join(DATA_RAW, "historical_markets.json")
PRICES_FILE= os.path.join(DATA_RAW, "historical_prices.json")

# ─────────────────────────────────────────────────────────
# SCHÉMA COMMUN — chaque marché est normalisé vers ce format
# ─────────────────────────────────────────────────────────
def empty_record(cid):
    return {
        "condition_id":    cid,
        "question":        None,
        "end_date":        None,
        "resolved":        None,
        "yes_won":         None,          # True / False / None
        "yes_price_clob":  None,          # Prix réel pre-résolution
        "yes_price_final": None,          # Prix à résolution (0 ou 1)
        "volume":          0.0,
        "yes_token_id":    None,
        "no_token_id":     None,
        "city":            None,
        "target_c":        None,
        "modifier":        None,          # exact / below / above
        "sources":         [],
        "actual_temp_max":  None,
        "actual_temp_min":  None,
        "actual_precip_mm": None,
        "actual_yes":       None,
        "gsod_tmax":            None,
        "gsod_tmin":            None,
        "delta_openmeteo_gsod": None,
        "forecast_tmax_j2":    None,
        "forecast_precip_j2":  None,
        "forecast_vs_target":  None,
        "trend_2d":            None,
        "tmax_j1":             None,
        "tmax_j3":             None,
        "tmax_j7":             None,
        "trend_7d":            None,
        "trend_3d":            None,
        "delta_vs_target_j1":  None,
        "delta_vs_target_j3":  None,
        "delta_vs_target_j7":  None,
        "era5_tmax":           None,
        "era5_tmin":           None,
        "delta_era5_openmeteo": None,
        "dune_yes_price_72h":  None,
        "dune_yes_price_24h":  None,
        "dune_outcome":        None,
        "dune_trade_count":    None,
        "dune_volume_usd":     None,
        "price_at_6h":         None,
        "price_at_24h":        None,
        "price_at_72h":        None,
        "price_at_168h":       None,
        "price_momentum_72h":  None,           # liste des sources qui ont contribué
    }

def merge_into(master, update, source_name):
    for k, v in update.items():
        if k == "sources":
            continue
        # Volume : prend le max des sources
        if k == "volume" and v:
            master[k] = max(float(master.get(k) or 0), float(v or 0))
            continue
        if v is not None and master.get(k) is None:
            master[k] = v
    if source_name not in master["sources"]:
        master["sources"].append(source_name)
    return master

# ─────────────────────────────────────────────────────────
# PARSER DE QUESTION
# ─────────────────────────────────────────────────────────
def parse_question(question):
    """Extrait city, target_c, modifier depuis la question."""
    if not question:
        return None, None, None
    m = re.search(
        r"temperature in ([A-Za-z\s]+?) be "
        r"(\d+(?:\.\d+)?)\s*°([CF])"
        r"(\s+or\s+(?:below|above))?",
        question, re.IGNORECASE
    )
    if not m:
        return None, None, None
    city = m.group(1).strip()
    temp = float(m.group(2))
    unit = m.group(3).upper()
    mod  = (m.group(4) or "").strip().lower()
    if unit == "F":
        temp = round((temp - 32) * 5 / 9, 2)
    modifier = "below" if "below" in mod else "above" if "above" in mod else "exact"
    return city, temp, modifier

def parse_outcome_prices(op):
    op_str = str(op or "")
    # Extrait tous les floats
    nums = re.findall(r"[\d]+\.?[\d]*", op_str)
    if len(nums) >= 2:
        try:
            v0, v1 = float(nums[0]), float(nums[1])
            if v0 > 0.9:
                return True, 1.0   # YES a gagné
            elif v1 > 0.9:
                return False, 0.0  # NO a gagné
        except:
            pass
    return None, None

# ─────────────────────────────────────────────────────────
# SOURCE 1 — HuggingFace (16,051 marchés historiques)
# ─────────────────────────────────────────────────────────
def load_huggingface(master_db):
    if not os.path.exists(HF_FILE):
        print("  ⚠️  HuggingFace : weather_complete.json absent")
        return 0
    with open(HF_FILE, encoding="utf-8") as f:
        hf = json.load(f)
    added = updated = 0
    for m in hf:
        cid = m.get("condition_id", "")
        if not cid:
            continue
        raw = m.get("_raw_market", {})
        raw_op = (m.get("_raw_market") or {}).get("outcome_prices")
        yes_won, yes_final = parse_outcome_prices(raw_op or m.get("final_yes_price"))
        city, target_c, modifier = parse_question(m.get("question", ""))
        t1 = str(raw.get("token1") or "")
        t2 = str(raw.get("token2") or "")
        a1 = str(raw.get("answer1") or "").lower()
        yes_tok = t1 if a1 == "yes" else t2
        no_tok  = t2 if yes_tok == t1 else t1
        update = {
            "question":        m.get("question"),
            "end_date":        str(m.get("end_date", ""))[:10],
            "resolved":        bool(m.get("resolved")),
            "yes_won":         yes_won,
            "yes_price_final": yes_final,
            "volume":          float(m.get("volume") or 0),
            "yes_token_id":    yes_tok or None,
            "no_token_id":     no_tok or None,
            "city":            city,
            "target_c":        target_c,
            "modifier":        modifier,
        }
        if cid not in master_db:
            master_db[cid] = empty_record(cid)
            added += 1
        else:
            updated += 1
        merge_into(master_db[cid], update, "huggingface")
    print(f"  ✅ HuggingFace : +{added} nouveaux | {updated} enrichis")
    return added

# ─────────────────────────────────────────────────────────
# SOURCE 2 — history_scraper cache (Gamma API + CLOB prices)
# ─────────────────────────────────────────────────────────
def load_gamma_cache(master_db):
    added = updated = 0
    # Marchés
    if os.path.exists(HIST_FILE):
        with open(HIST_FILE, encoding="utf-8") as f:
            hist = json.load(f)
        for m in hist:
            cid = m.get("condition_id", "")
            if not cid:
                continue
            city, target_c, modifier = parse_question(m.get("question", ""))
            update = {
                "question":      m.get("question"),
                "end_date":      str(m.get("end_date", ""))[:10],
                "resolved":      bool(m.get("resolved")),
                "yes_won":       m.get("yes_won"),
                "volume":        float(m.get("volume") or 0),
                "yes_token_id":  m.get("yes_token_id"),
                "no_token_id":   m.get("no_token_id"),
                "city":          city,
                "target_c":      target_c,
                "modifier":      modifier,
            }
            if cid not in master_db:
                master_db[cid] = empty_record(cid)
                added += 1
            else:
                updated += 1
            merge_into(master_db[cid], update, "gamma_api")
        print(f"  ✅ Gamma cache : +{added} nouveaux | {updated} enrichis")

    # Prix CLOB
    prices_added = 0
    if os.path.exists(PRICES_FILE):
        with open(PRICES_FILE, encoding="utf-8") as f:
            prices = json.load(f)
        for p in prices:
            cid = p.get("condition_id", "")
            yp  = p.get("yes_price")
            cid_norm = cid.lower().lstrip("0x")
            match = next((k for k in master_db if k.lower().lstrip("0x") == cid_norm), None)
            if match and yp:
                cid = match
            if cid and yp and cid in master_db:
                if master_db[cid]["yes_price_clob"] is None:
                    master_db[cid]["yes_price_clob"] = float(yp)
                    if "clob_prices" not in master_db[cid]["sources"]:
                        master_db[cid]["sources"].append("clob_prices")
                    prices_added += 1
        print(f"  ✅ CLOB prices : {prices_added} prix ajoutés")
    return added

# ─────────────────────────────────────────────────────────
# SOURCE 3 — Gamma API live (marchés actifs non couverts)
# ─────────────────────────────────────────────────────────
def load_gamma_live(master_db):
    import urllib.request
    try:
        url = ("https://gamma-api.polymarket.com/markets"
               "?active=true&closed=false&limit=500"
               "&order=volume&ascending=false")
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            markets = json.load(r)
    except Exception as e:
        print(f"  ⚠️  Gamma live : {e}")
        return 0
    added = updated = 0
    for m in markets:
        q = m.get("question","").lower()
        if "temperature" not in q and "hurricane" not in q and "precipitation" not in q:
            continue
        cid = m.get("conditionId","")
        if not cid:
            continue
        city, target_c, modifier = parse_question(m.get("question",""))
        try:
            prices = [float(x) for x in json.loads(m.get("outcomePrices","[]"))]
        except:
            prices = []
        update = {
            "question":      m.get("question"),
            "end_date":      str(m.get("endDate",""))[:10],
            "resolved":      bool(m.get("closed")),
            "yes_won":       None,
            "yes_price_clob": prices[0] if prices else None,
            "volume":        float(m.get("volume") or 0),
            "city":          city,
            "target_c":      target_c,
            "modifier":      modifier,
        }
        if cid not in master_db:
            master_db[cid] = empty_record(cid)
            added += 1
        else:
            updated += 1
        merge_into(master_db[cid], update, "gamma_live")
    print(f"  ✅ Gamma live  : +{added} nouveaux | {updated} enrichis")
    return added
    

# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────
def load_quant_prices(master_db):
    quant_file = os.path.join(DATA_RAW, "hf", "quant_weather_prices.json")
    if not os.path.exists(quant_file):
        print("  ⚠️  quant_weather_prices.json absent")
        return 0
    with open(quant_file, encoding="utf-8") as f:
        prices = json.load(f)
    added = 0
    for p in prices:
        cid  = p.get("condition_id", "")
        vwap = p.get("yes_price_vwap")
        if not cid or vwap is None:
            continue
        if cid in master_db and master_db[cid]["yes_price_clob"] is None:
            master_db[cid]["yes_price_clob"] = round(float(vwap), 6)
            if "quant_parquet" not in master_db[cid]["sources"]:
                master_db[cid]["sources"].append("quant_parquet")
            added += 1
    print(f"  ✅ quant.parquet   : {added} prix ajoutés")
    return added
def load_weather_actuals(master_db):
    actuals_file = os.path.join(DATA_RAW, "historical_weather_actuals.json")
    if not os.path.exists(actuals_file):
        print("  ⚠️  historical_weather_actuals.json absent")
        return 0
    with open(actuals_file, encoding="utf-8") as f:
        actuals = json.load(f)
    added = 0
    for a in actuals:
        cid = a.get("condition_id", "")
        if cid not in master_db:
            continue
        m = master_db[cid]
        if m.get("actual_temp_max") is None:
            m["actual_temp_max"]  = a.get("actual_temp_max")
            m["actual_temp_min"]  = a.get("actual_temp_min")
            m["actual_precip_mm"] = a.get("actual_precip_mm")
            m["actual_yes"]       = a.get("actual_yes")
            if "weather_actuals" not in m["sources"]:
                m["sources"].append("weather_actuals")
            added += 1
    print(f"  ✅ weather_actuals  : {added} températures réelles ajoutées")
    return added
def load_noaa_gsod(master_db):
    gsod_file = os.path.join(DATA_RAW, "noaa_gsod_actuals.json")
    if not os.path.exists(gsod_file):
        print("  ⚠️  noaa_gsod_actuals.json absent")
        return 0
    with open(gsod_file, encoding="utf-8") as f:
        gsod = json.load(f)
    added = 0
    for g in gsod:
        cid = g.get("condition_id", "")
        if cid not in master_db:
            continue
        m = master_db[cid]
        if m.get("gsod_tmax") is None and g.get("gsod_tmax") is not None:
            m["gsod_tmax"]            = g.get("gsod_tmax")
            m["gsod_tmin"]            = g.get("gsod_tmin")
            m["delta_openmeteo_gsod"] = g.get("delta_openmeteo_gsod")
            if "noaa_gsod" not in m["sources"]:
                m["sources"].append("noaa_gsod")
            added += 1
    print(f"  ✅ NOAA GSOD       : {added} températures stations ajoutées")
    return added
def load_forecast_at_trade(master_db):
    f = os.path.join(DATA_RAW, "forecast_at_trade.json")
    if not os.path.exists(f):
        print("  ⚠️  forecast_at_trade.json absent")
        return 0
    with open(f, encoding="utf-8") as fp:
        data = json.load(fp)
    added = 0
    for d in data:
        cid = d.get("condition_id", "")
        if cid not in master_db:
            continue
        m = master_db[cid]
        if m.get("forecast_tmax_j2") is None:
            m["forecast_tmax_j2"]   = d.get("forecast_tmax")
            m["forecast_precip_j2"] = d.get("forecast_precip")
            m["forecast_vs_target"] = d.get("forecast_vs_target")
            m["trend_2d"]           = d.get("trend_2d")
            if "forecast_j2" not in m["sources"]:
                m["sources"].append("forecast_j2")
            added += 1
    print(f"  ✅ forecast J-2    : {added} prévisions ajoutées")
    return added
def load_multi_horizon(master_db):
    f = os.path.join(DATA_RAW, "multi_horizon_forecasts.json")
    if not os.path.exists(f):
        print("  ⚠️  multi_horizon_forecasts.json absent")
        return 0
    with open(f, encoding="utf-8") as fp:
        data = json.load(fp)
    added = 0
    for d in data:
        cid = d.get("condition_id", "")
        if cid not in master_db:
            continue
        m = master_db[cid]
        if m.get("tmax_j1") is None:
            for key in ["tmax_j1","tmin_j1","prec_j1","wind_j1","hum_j1",
                        "tmax_j3","tmin_j3","prec_j3","wind_j3","hum_j3",
                        "tmax_j7","tmin_j7","prec_j7","wind_j7","hum_j7",
                        "trend_7d","trend_3d",
                        "delta_vs_target_j1","delta_vs_target_j3","delta_vs_target_j7"]:
                if d.get(key) is not None:
                    m[key] = d[key]
            if "multi_horizon" not in m["sources"]:
                m["sources"].append("multi_horizon")
            added += 1
    print(f"  ✅ multi_horizon   : {added} prévisions multi-horizons ajoutées")
    return added
def load_era5(master_db):
    f = os.path.join(DATA_RAW, "era5_actuals.json")
    if not os.path.exists(f):
        print("  ⚠️  era5_actuals.json absent")
        return 0
    with open(f, encoding="utf-8") as fp:
        data = json.load(fp)
    added = 0
    for d in data:
        cid = d.get("condition_id", "")
        if cid not in master_db:
            continue
        m = master_db[cid]
        if m.get("era5_tmax") is None and d.get("era5_tmax") is not None:
            m["era5_tmax"]            = d["era5_tmax"]
            m["era5_tmin"]            = d["era5_tmin"]
            m["delta_era5_openmeteo"] = d.get("delta_era5_openmeteo")
            if "era5" not in m["sources"]:
                m["sources"].append("era5")
            added += 1
    print(f"  ✅ ERA5            : {added} températures ECMWF ajoutées")
    return added
def load_dune_prices(master_db):
    f = os.path.join(DATA_RAW, "dune", "weather_trades.json")
    if not os.path.exists(f):
        print("  ⚠️  dune/weather_trades.json absent")
        return 0
    with open(f, encoding="utf-8") as fp:
        data = json.load(fp)
    found = 0
    for d in data:
        cid = d.get("condition_id", "")
        if not cid or cid not in master_db:
            continue
        m = master_db[cid]
        if m.get("dune_yes_price_72h") is None:
            m["dune_yes_price_72h"] = d.get("yes_price_72h")
            m["dune_yes_price_24h"] = d.get("yes_price_24h")
            m["dune_outcome"]       = d.get("outcome")
            m["dune_trade_count"]   = d.get("trade_count")
            m["dune_volume_usd"]    = d.get("volume_usd")
            if "dune" not in m["sources"]:
                m["sources"].append("dune")
            found += 1
    print(f"  ✅ Dune prices     : {found} prix 72h/24h ajoutés")
    return found
def load_price_timeseries(master_db):
    hourly_file = os.path.join(DATA_RAW, "dune", "market_prices_hourly_local.json")
    if not os.path.exists(hourly_file):
        print("  ⚠️  market_prices_hourly_local.json absent")
        return 0
    with open(hourly_file, encoding="utf-8") as f:
        rows = json.load(f)
    # Groupe par condition_id
    by_cid = {}
    for r in rows:
        cid = r.get("condition_id", "")
        if cid not in by_cid:
            by_cid[cid] = {}
        h = int(r.get("hours_before_expiry") or 0)
        by_cid[cid][h] = float(r.get("price_avg") or 0)
    added = 0
    for cid, prices in by_cid.items():
        if cid not in master_db:
            continue
        m = master_db[cid]
        # Prix aux horizons clés
        m["price_at_6h"]   = prices.get(6)
        m["price_at_24h"]  = prices.get(24)
        m["price_at_72h"]  = prices.get(72)
        m["price_at_168h"] = prices.get(168)
        # Momentum : évolution prix entre J-7 et J-1
        p168 = prices.get(168)
        p24  = prices.get(24)
        if p168 and p24:
            m["price_momentum_72h"] = round(p24 - p168, 4)
        if "timeseries" not in m["sources"]:
            m["sources"].append("timeseries")
        added += 1
    print(f"  ✅ timeseries      : {added} séries temporelles ajoutées")
    return added
if __name__ == "__main__":
    # Charge master existant si dispo
    if os.path.exists(MASTER):
        with open(MASTER, encoding="utf-8") as f:
            master_list = json.load(f)
        master_db = {m["condition_id"]: m for m in master_list}
        print(f"📂 Master existant : {len(master_db)} marchés\n")
    else:
        master_db = {}
        print("📂 Nouveau master\n")

    print("🔗 Chargement des sources...\n")
    load_huggingface(master_db)
    load_gamma_cache(master_db)
    load_gamma_live(master_db)
    load_quant_prices(master_db)
    load_weather_actuals(master_db)
    load_noaa_gsod(master_db)
    load_forecast_at_trade(master_db)
    load_multi_horizon(master_db)
    load_era5(master_db)
    load_dune_prices(master_db)
    load_price_timeseries(master_db)

    # Sauvegarde
    master_list = list(master_db.values())
    master_list.sort(key=lambda x: x.get("end_date") or "")

    with open(MASTER, "w", encoding="utf-8") as f:
        json.dump(master_list, f, indent=2, default=str)

    # Stats finales
    resolved   = [m for m in master_list if m.get("resolved") is True]
    with_result= [m for m in resolved if m.get("yes_won") is not None]
    with_price = [m for m in resolved if m["yes_price_clob"] is not None]
    temp_mkts  = [m for m in master_list if m.get("target_c") is not None]

    print(f"\n{'═'*60}")
    print(f"  MASTER WEATHER DATABASE")
    print(f"{'═'*60}")
    print(f"  Total marchés météo       : {len(master_list):,}")
    print(f"  ├─ Température parsée     : {len(temp_mkts):,}")
    print(f"  ├─ Résolus                : {len(resolved):,}")
    print(f"  ├─ Résultat YES/NO connu  : {len(with_result):,}")
    with_actuals = [m for m in master_list if m.get("actual_temp_max") is not None]
    print(f"  ├─ Avec prix CLOB réel    : {len(with_price):,}  ← backtest fiable")
    print(f"  ├─ Avec temp réelle       : {len(with_actuals):,}  ← training IA")
    with_gsod = [m for m in master_list if m.get("gsod_tmax") is not None]
    print(f"  ├─ Avec GSOD stations     : {len(with_gsod):,}  ← validation croisée")
    with_forecast = [m for m in master_list if m.get("forecast_tmax_j2") is not None]
    print(f"  ├─ Avec prévision J-2     : {len(with_forecast):,}  ← feature IA")
    with_mh = [m for m in master_list if m.get("tmax_j1") is not None]
    print(f"  ├─ Avec multi-horizons    : {len(with_mh):,}  ← feature IA")
    with_era5 = [m for m in master_list if m.get("era5_tmax") is not None]
    print(f"  ├─ Avec ERA5 ECMWF        : {len(with_era5):,}  ← température précise")
    print(f"  └─ Sources croisées (2+)  : {sum(1 for m in master_list if len(m['sources'])>=2):,}")
    print(f"  Volume total              : ${sum(m['volume'] for m in master_list):,.0f}")
    print(f"  Fichier                   : {MASTER}")
    print(f"{'═'*60}")

    