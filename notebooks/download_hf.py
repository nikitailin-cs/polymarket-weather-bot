"""
Télécharge markets.parquet depuis HuggingFace via HTTP direct.
"""
import urllib.request, os, sys

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "hf")
os.makedirs(OUT_DIR, exist_ok=True)
OUT_FILE = os.path.join(OUT_DIR, "markets.parquet")

# URL directe HuggingFace (pas besoin d'auth pour dataset public)
URL = "https://huggingface.co/datasets/SII-WANGZJ/Polymarket_data/resolve/main/markets.parquet"

if os.path.exists(OUT_FILE):
    size = os.path.getsize(OUT_FILE) / 1024 / 1024
    print(f"✅ Déjà téléchargé : {size:.1f} MB")
    sys.exit(0)

print(f"Téléchargement markets.parquet (~68MB)...")
print(f"Destination : {OUT_FILE}\n")

def reporthook(block, block_size, total):
    done = block * block_size
    if total > 0:
        pct = min(100, done * 100 / total)
        mb  = done / 1024 / 1024
        sys.stdout.write(f"\r  {pct:.1f}%  {mb:.1f} MB")
        sys.stdout.flush()

try:
    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(URL, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r, open(OUT_FILE, "wb") as f:
        total = int(r.headers.get("Content-Length", 0))
        block = 65536
        downloaded = 0
        while True:
            chunk = r.read(block)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = min(100, downloaded * 100 / total)
                sys.stdout.write(f"\r  {pct:.1f}%  {downloaded/1024/1024:.1f} MB / {total/1024/1024:.1f} MB")
                sys.stdout.flush()
    print(f"\n✅ Téléchargé : {os.path.getsize(OUT_FILE)/1024/1024:.1f} MB")
except Exception as e:
    print(f"\n❌ Erreur : {e}")
    # Nettoie le fichier partiel
    if os.path.exists(OUT_FILE):
        os.remove(OUT_FILE)
    sys.exit(1)