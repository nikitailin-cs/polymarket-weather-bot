#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test d'intégration **réseau réel** : NOMADS GFS (fichiers du jour sur
https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/) via :mod:`noaa_engine`.

Usage (depuis la racine du repo ou ce dossier) ::
    python3 bots/bot1/noaa_nomads_live/run_nomads_live_test.py
    python3 bots/bot1/noaa_nomads_live/run_nomads_live_test.py --lon -73.98 --lat 40.75
    python3 run_nomads_live_test.py --forecast-hours 3 --http-debug
"""
from __future__ import annotations

import argparse
import io
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# noaa_engine vit dans le dossier voisin « Long Term »
_LONG_TERM = Path(__file__).resolve().parent.parent / "Long Term"
if str(_LONG_TERM) not in sys.path:
    sys.path.insert(0, str(_LONG_TERM))

import noaa_engine  # noqa: E402


def _configure_logging(*, http_debug: bool) -> logging.Logger:
    """Console : DEBUG massif + option trace HTTP bas niveau (http.client)."""
    fmt = "%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(level=logging.DEBUG, format=fmt, datefmt=datefmt, force=True)

    log = logging.getLogger("nomads_live_test")
    log.info("racine script = %s", Path(__file__).resolve().parent)
    log.info("noaa_engine chargé depuis = %s", Path(noaa_engine.__file__).resolve())
    log.debug("sys.path[0] (Long Term) = %s", _LONG_TERM)

    # Bruit utile : constantes réseau du module
    log.debug("BASE_URL_PRIORITY = %s", noaa_engine.BASE_URL_PRIORITY)
    log.debug("BASE_URL_BACKUP   = %s", noaa_engine.BASE_URL_BACKUP)
    log.debug("DEFAULT_HEAD_TIMEOUT_SEC = %s", noaa_engine.DEFAULT_HEAD_TIMEOUT_SEC)

    if http_debug:
        try:
            import http.client as http_client

            http_client.HTTPConnection.debuglevel = 1
            log.warning("HTTP debuglevel=1 activé (très verbeux sur stderr)")
        except Exception as e:
            log.warning("Impossible d'activer http.client debug : %s", e)

    return log


def main() -> int:
    p = argparse.ArgumentParser(description="Test NOMADS live + noaa_engine (logs massifs).")
    p.add_argument("--forecast-hours", type=int, default=3, help="Échéance fXXX (ex. 3 → f003).")
    p.add_argument(
        "--max-chunk-mb",
        type=float,
        default=15.0,
        help="Plafond taille segment GRIB (Mo) avant de skip le GET Range.",
    )
    p.add_argument(
        "--http-debug",
        action="store_true",
        help="Active http.client.HTTPConnection.debuglevel=1 (très verbeux).",
    )
    p.add_argument(
        "--skip-wgrib2",
        action="store_true",
        help="Ne pas invoquer wgrib2 même si présent.",
    )
    p.add_argument(
        "--lon",
        type=float,
        default=None,
        help="Avec --lat : utilise run_wgrib2_scalpel_at_point (1 valeur, pas de CSV grille entière).",
    )
    p.add_argument(
        "--lat",
        type=float,
        default=None,
        help="Avec --lon : extraction au point GPS.",
    )
    args = p.parse_args()

    if (args.lon is None) ^ (args.lat is None):
        print("Erreur : fournir --lon et --lat ensemble, ou aucun des deux.", file=sys.stderr)
        return 2

    log = _configure_logging(http_debug=args.http_debug)
    fh = max(0, int(args.forecast_hours))
    max_chunk = int(args.max_chunk_mb * 1024 * 1024)

    t0 = time.monotonic()
    now = datetime.now(timezone.utc)
    log.info("========== DÉBUT TEST NOMADS ==========")
    log.info("UTC = %s", now.isoformat())
    log.info("forecast_hours = %s (f%03d)", fh, fh)

    hit = None
    chosen_run = None
    for run in noaa_engine.iter_candidate_runs(now_utc=now, max_runs=24):
        log.info("— candidat run day=%s cycle_utc=%s", run.day, run.cycle_utc)
        idx_nomads = noaa_engine.build_gfs_idx_url(
            run.day, run.cycle_utc, fh, base=noaa_engine.BASE_URL_PRIORITY
        )
        log.info("  URL .idx NOMADS : %s", idx_nomads)

        t_probe = time.monotonic()
        hit = noaa_engine.probe_idx_hybrid(run, fh)
        log.debug("  probe_idx_hybrid durée ≈ %.3fs", time.monotonic() - t_probe)

        if hit:
            chosen_run = run
            log.info(
                "  OK probe — source=%s http=%s url=%s last_modified=%s",
                hit.source,
                hit.status_code,
                hit.url,
                hit.last_modified,
            )
            break
        log.warning("  probe_idx_hybrid → None, essai suivant")

    if not hit or chosen_run is None:
        log.error("Aucun .idx trouvé (NOMADS/S3) sur les runs candidats.")
        return 1

    pgrb2_url = noaa_engine.build_gfs_pgrb2_url(
        chosen_run.day, chosen_run.cycle_utc, fh, base=noaa_engine.GFS_PROD_BASE
    )
    log.info("URL pgrb2 associée : %s", pgrb2_url)

    try:
        log.info("— Étape GET .idx (texte)")
        t1 = time.monotonic()
        idx_text = noaa_engine.fetch_idx_text(hit.url)
        log.info("  fetch_idx_text : %.3fs, %s caractères", time.monotonic() - t1, len(idx_text))
        lines = idx_text.splitlines()
        log.info("  lignes = %s", len(lines))
        log.debug("  [extrait 12 premières lignes]\n%s", "\n".join(lines[:12]))

        log.info("— Étape parse_idx_wgrib2")
        parsed = noaa_engine.parse_idx_wgrib2(idx_text)
        log.info("  enregistrements = %s", len(parsed))
        if parsed:
            log.debug("  [0] = %s", parsed[0])
            log.debug("  [-1] = %s", parsed[-1])

        log.info("— Étape HEAD Content-Length (pgrb2)")
        t2 = time.monotonic()
        total = noaa_engine.head_get_content_length(pgrb2_url)
        log.info("  head_get_content_length : %.3fs → %s", time.monotonic() - t2, total)

        log.info("— Étape byte_range TMP 2 m")
        span = noaa_engine.byte_range_for_variable_and_level(
            idx_text,
            variable_must_contain=":TMP:",
            level_must_contain="2 m above ground",
            pgrb2_total_bytes=total,
        )
        log.info("  span = %s", span)

        if span is None:
            log.warning("Pas de plage TMP 2 m — arrêt sans GET Range.")
            log.info("========== FIN TEST (partiel) en %.2fs ==========", time.monotonic() - t0)
            return 0

        a, b = span
        nbytes = b - a + 1
        log.info("  segment = %s octets (~%.3f Mo)", nbytes, nbytes / (1024 * 1024))

        if nbytes > max_chunk:
            log.warning(
                "Segment > plafond %s Mo — GET Range ignoré (sécurité RAM).",
                args.max_chunk_mb,
            )
            log.info("========== FIN TEST (sans chunk) en %.2fs ==========", time.monotonic() - t0)
            return 0

        log.info("— Étape GET Range pgrb2 [%s..%s]", a, b)
        t3 = time.monotonic()
        chunk = noaa_engine.get_range_bytes(pgrb2_url, a, b)
        log.info("  get_range_bytes : %.3fs, reçu %s octets", time.monotonic() - t3, len(chunk))
        log.debug("  premiers 32 octets (hex) = %s", chunk[:32].hex())

        bio = io.BytesIO(chunk)
        log.debug("BytesIO len avant release = %s", len(bio.getvalue()))
        noaa_engine.release_bytesio(bio)
        log.debug("BytesIO len après release_bytesio = %s", len(bio.getvalue()))

        wgrib_code: int | None = None
        if args.skip_wgrib2:
            log.info("— wgrib2 ignoré (--skip-wgrib2)")
        else:
            wg = shutil.which("wgrib2")
            if not wg:
                log.warning("wgrib2 absent du PATH — étape scalpel ignorée")
            else:
                t4 = time.monotonic()
                if args.lon is not None and args.lat is not None:
                    log.info(
                        "— Étape run_wgrib2_scalpel_at_point (stdin) — %s lon=%s lat=%s",
                        wg,
                        args.lon,
                        args.lat,
                    )
                    code, val, out, err = noaa_engine.run_wgrib2_scalpel_at_point(
                        chunk,
                        lon=args.lon,
                        lat=args.lat,
                        match_substring=":TMP:2 m above ground:",
                        timeout_sec=120.0,
                    )
                    wgrib_code = code
                    log.info(
                        "  wgrib2 fin en %.3fs — code=%s valeur=%r stdout=%s stderr=%s",
                        time.monotonic() - t4,
                        code,
                        val,
                        len(out),
                        len(err),
                    )
                    log.debug("  stdout\n%s", out[:8000])
                    if err:
                        log.debug("  stderr\n%s", err[:4000])
                else:
                    log.info(
                        "— Étape run_wgrib2_scalpel (stdin, -csv grille entière) — %s",
                        wg,
                    )
                    log.warning(
                        "Sans --lon/--lat, le CSV peut faire des dizaines de Mo (tous les points). "
                        "Préfère --lon X --lat Y pour un test léger.",
                    )
                    code, out, err = noaa_engine.run_wgrib2_scalpel(
                        chunk,
                        match_substring=":TMP:2 m above ground:",
                        timeout_sec=120.0,
                    )
                    wgrib_code = code
                    log.info(
                        "  wgrib2 fin en %.3fs — code=%s stdout=%s stderr=%s",
                        time.monotonic() - t4,
                        code,
                        len(out),
                        len(err),
                    )
                    if len(out) > 5_000_000:
                        log.warning(
                            "Sortie wgrib2 très volumineuse (%s car.) — RAM / disque : "
                            "utilise --lon/--lat pour l’extraction au point.",
                            len(out),
                        )
                    log.debug("  stdout (4000 prem. car.)\n%s", out[:4000])
                    if err:
                        log.debug("  stderr\n%s", err[:4000])

        if wgrib_code is not None and wgrib_code != 0:
            log.error("wgrib2 a retourné code=%s — échec", wgrib_code)
            log.info("========== FIN TEST ÉCHEC wgrib2 en %.2fs ==========", time.monotonic() - t0)
            return 1

        log.info("========== FIN TEST OK en %.2fs ==========", time.monotonic() - t0)
        return 0
    except Exception:
        log.exception("Échec pipeline (exception ci-dessous)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
