"""
Calibrateur de biais des modèles météo.
Compare nos prédictions passées aux températures réelles observées.
Permet de corriger le biais systématique par ville/saison.

Fonctionnement :
1. À chaque prédiction, on enregistre (ville, date, temp_prévue)
2. Le lendemain, on récupère la temp réelle via Open-Meteo Archive
3. On calcule le biais = prévision - réalité
4. On applique ce biais en correction aux futures prédictions
"""
from __future__ import annotations
import json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List
from pathlib import Path

_bias_override = os.getenv("MODEL_BIAS_FILE", "").strip()
BIAS_FILE = (
    Path(_bias_override)
    if _bias_override
    else Path(__file__).parent.parent.parent / "data" / "processed" / "model_bias.json"
)
BIAS_FILE.parent.mkdir(parents=True, exist_ok=True)

# Nombre minimum d'observations pour appliquer la correction
MIN_OBSERVATIONS = 5
# Fenêtre glissante : on ne garde que les N dernières observations par ville
MAX_OBSERVATIONS = 50


def _load_bias() -> Dict:
    if not BIAS_FILE.exists():
        return {}
    try:
        with open(BIAS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_bias(data: Dict) -> None:
    tmp = BIAS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(BIAS_FILE)


def record_prediction(city: str, date_str: str, predicted_temp: float) -> None:
    """
    Enregistre une prédiction pour calibration future.
    Appelé au moment du trade.
    """
    bias = _load_bias()
    key  = city.lower().strip()

    if key not in bias:
        bias[key] = {"predictions": [], "bias_mean": 0.0, "n": 0}

    bias[key]["predictions"].append({
        "date":      date_str,
        "predicted": round(predicted_temp, 2),
        "actual":    None,   # Sera rempli par update_actuals()
        "error":     None,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    })

    # Garde les MAX_OBSERVATIONS dernières
    bias[key]["predictions"] = bias[key]["predictions"][-MAX_OBSERVATIONS:]
    _save_bias(bias)


def update_actuals() -> int:
    """
    Pour chaque prédiction sans température réelle, essaie de récupérer
    la valeur observée depuis Open-Meteo Archive.
    Retourne le nombre de mises à jour effectuées.
    """
    from signals.sources.open_meteo import geocode
    import urllib.request, urllib.parse

    bias    = _load_bias()
    updated = 0
    now     = datetime.now(timezone.utc)

    for city_key, city_data in bias.items():
        coords = geocode(city_key.title())
        if coords is None:
            continue
        lat, lon = coords

        for pred in city_data["predictions"]:
            if pred["actual"] is not None:
                continue  # Déjà mis à jour

            pred_date = datetime.strptime(pred["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if pred_date >= now:
                continue  # Pas encore passé

            # Récupère la température réelle
            try:
                url = (
                    f"https://archive-api.open-meteo.com/v1/archive"
                    f"?latitude={lat}&longitude={lon}"
                    f"&start_date={pred['date']}&end_date={pred['date']}"
                    f"&daily=temperature_2m_max&timezone=auto"
                )
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())
                temps = data.get("daily", {}).get("temperature_2m_max", [])
                if temps and temps[0] is not None:
                    actual        = float(temps[0])
                    pred["actual"] = round(actual, 2)
                    pred["error"]  = round(pred["predicted"] - actual, 2)
                    updated += 1
            except Exception:
                continue

        # Recalcule le biais moyen pour cette ville
        errors = [p["error"] for p in city_data["predictions"]
                  if p["error"] is not None]
        if errors:
            city_data["bias_mean"] = round(sum(errors) / len(errors), 3)
            city_data["bias_std"]  = round(
                (sum((e - city_data["bias_mean"]) ** 2 for e in errors) / len(errors)) ** 0.5, 3
            )
            city_data["n"] = len(errors)

    _save_bias(bias)
    return updated


def _sigma_lookup_key_variants(key: str) -> List[str]:
    """Ordre de recherche dans ``model_bias.json`` (ville minuscule, ICAO, WMO, etc.)."""
    k0 = str(key).lower().strip()
    if not k0:
        return []
    out: List[str] = []
    seen: set[str] = set()

    def add(x: str) -> None:
        x = x.strip()
        if not x or x in seen:
            return
        seen.add(x)
        out.append(x)

    add(k0)
    add(k0.upper())
    # Marché condition_id souvent long ; préfixe ville si format "ville_…"
    if "_" in k0:
        add(k0.split("_", 1)[0])
    return out


def get_sigma_hint_for_key(key: str) -> Optional[float]:
    """
    Pour bot1 / autres pipelines : si la clé (ville, ICAO, etc.) existe dans le fichier
    biais avec assez d'observations, retourne un sigma gaussien heuristique dérivé de
    ``bias_std`` (écart-type des erreurs passées), borné pour éviter des extrêmes.
    Retourne None si pas de données exploitables.

    Essaie plusieurs variantes (:func:`_sigma_lookup_key_variants`) pour matcher des entrées
    ``KJFK`` vs ``kjfk`` ou une clé ``market_id``.
    """
    variants = _sigma_lookup_key_variants(key)
    if not variants:
        return None
    bias = _load_bias()
    for k in variants:
        data = bias.get(k)
        if not isinstance(data, dict):
            continue
        n = int(data.get("n", 0) or 0)
        if n < MIN_OBSERVATIONS:
            continue
        try:
            std = float(data.get("bias_std", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if std <= 0:
            continue
        return max(0.5, min(8.0, std))
    return None


def get_bias_correction(city: str) -> Optional[float]:
    """
    Retourne la correction de biais à appliquer pour une ville (en °C).
    Ex: si on sur-estime Madrid de +1.5°C en moyenne → retourne -1.5
    Retourne None si pas assez de données.
    """
    bias    = _load_bias()
    key     = city.lower().strip()
    data    = bias.get(key, {})
    n       = data.get("n", 0)

    if n < MIN_OBSERVATIONS:
        return None

    return -data.get("bias_mean", 0.0)  # Correction inverse du biais


def get_bias_summary() -> Dict:
    """Retourne un résumé des biais par ville."""
    bias    = _load_bias()
    summary = {}
    for city, data in bias.items():
        n = data.get("n", 0)
        if n > 0:
            summary[city] = {
                "n":          n,
                "bias_mean":  data.get("bias_mean", 0.0),
                "bias_std":   data.get("bias_std", 0.0),
                "correction": round(-data.get("bias_mean", 0.0), 3),
            }
    return summary


def print_bias_report() -> None:
    summary = get_bias_summary()
    if not summary:
        print("  Pas encore assez de données pour calculer les biais.")
        print(f"  (Minimum {MIN_OBSERVATIONS} observations par ville requises)")
        return

    print(f"\n{'═'*60}")
    print(f"  RAPPORT DE CALIBRATION DES MODÈLES")
    print(f"{'═'*60}")
    print(f"  {'Ville':<20} {'N':>4} {'Biais':>8} {'Std':>8} {'Correction':>10}")
    print(f"  {'─'*56}")
    for city, d in sorted(summary.items()):
        bias_str = f"{d['bias_mean']:+.2f}°C"
        corr_str = f"{d['correction']:+.2f}°C"
        print(f"  {city.title():<20} {d['n']:>4} {bias_str:>8} {d['bias_std']:>7.2f}°C {corr_str:>10}")
    print(f"{'═'*60}")
    print("  Biais positif = on sur-estime la température")
    print("  Correction = ajustement automatique appliqué aux futures prédictions")


if __name__ == "__main__":
    print("🔧 Mise à jour des températures réelles...")
    n = update_actuals()
    print(f"   {n} observations mises à jour")
    print_bias_report()