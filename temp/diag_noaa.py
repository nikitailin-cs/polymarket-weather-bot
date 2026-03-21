import urllib.request

# Test différentes URLs NOAA GSOD
urls = [
    "https://www.ncei.noaa.gov/data/global-summary-of-the-day/access/2025/037720-99999.csv",
    "https://www.ncei.noaa.gov/data/global-summary-of-the-day/access/2024/037720-99999.csv",
    "https://www1.ncdc.noaa.gov/pub/data/gsod/2025/gsod_2025.tar",
    "https://www.ncei.noaa.gov/access/services/data/v1?dataset=global-summary-of-the-day&stations=72503014732&startDate=2025-01-01&endDate=2025-01-05&format=csv",
]

for url in urls:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            content = r.read(500)
            print(f"✅ {url[:70]}")
            print(f"   {content[:200]}\n")
    except Exception as e:
        print(f"❌ {url[:70]}")
        print(f"   {e}\n")