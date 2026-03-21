"""
Stream le dataset HuggingFace sans tout télécharger.
Filtre uniquement les marchés météo température et sauvegarde.
"""
import os, json, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from datasets import load_dataset

WEATHER_KW = [
    "temperature", "celsius", "fahrenheit", "highest temp", "lowest temp",
    "rain", "rainfall", "snow", "snowfall", "precipitation",
    "hurricane", "storm", "tornado", "flood", "wind speed", "humidity"
]

OUT = "data/raw/hf/weather_markets_hf.json"
os.makedirs("data/raw/hf", exist_ok=True)

print("🌐 Connexion streaming HuggingFace (sans téléchargement complet)...")
print("   Token HF requis — connecte-toi sur huggingface.co si besoin\n")

token = os.getenv("HF_TOKEN", None)

# Streaming = lit chunk par chunk sans tout télécharger
ds = load_dataset(
    "SII-WANGZJ/Polymarket_data",
    split="train",
    streaming=True,
    token=token
)

found    = []
scanned  = 0

print("🔍 Scan en cours (affiche progression toutes les 10 000 lignes)...\n")

for row in ds:
    scanned += 1
    q = (row.get("question") or row.get("title") or "").lower()

    if any(kw in q for kw in WEATHER_KW):
        found.append(dict(row))
        print(f"  ✅ [{scanned:>7}] {row.get('question','')[:80]}")

    if scanned % 10_000 == 0:
        print(f"  ... {scanned:,} lignes scannées | {len(found)} météo trouvés")

    # Stop si on a tout (le dataset a ~268K marchés)
    if scanned >= 300_000:
        break

print(f"\n✅ Scan terminé : {scanned:,} lignes | {len(found)} marchés météo")
print(f"Colonnes disponibles : {list(found[0].keys()) if found else 'N/A'}")

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(found, f, indent=2, default=str)

print(f"💾 Sauvegardé : {OUT}")