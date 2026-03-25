#!/usr/bin/env python3
"""
Test d’intégration local — logs massifs (NOAA bot1 → finance dry → METAR bot2).

- **Bot1 NOAA** : délègue à ``produce_trading_signals.py`` (déjà en DEBUG console).
  Passe ``--force --allow-outside-window --min-run-interval-sec 0`` pour ne pas être
  bloqué par le watchdog pendant les tests.

- **Finance** : lance ``finance_engine.py`` seulement si le manifeste NOAA a ``status=ok``
  (sauf ``--skip-finance``), avec ``FINANCE_ENGINE_DRY_RUN=1`` par défaut.

- **Bot2 RT** : exécute ``produce_rt_signals`` dans le même interpréteur après avoir
  forcé le logging root en DEBUG (avant import du module).

Usage (depuis la racine du repo) ::

    python temp/run_full_pipeline_verbose.py
    python temp/run_full_pipeline_verbose.py --log-file logs/pipeline_verbose.log
    python temp/run_full_pipeline_verbose.py --http-debug
    python temp/run_full_pipeline_verbose.py --noaa-only

Sortie : tout est recopié vers ``--log-file`` (défaut : ``temp/logs/pipeline_verbose_<ts>.log``).
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _banner(title: str, fp) -> None:
    line = "=" * 72
    msg = f"\n{line}\n  {title}\n{line}\n"
    print(msg, end="", flush=True)
    fp.write(msg)
    fp.flush()


def _stream_subprocess(cmd: list[str], env: dict[str, str], fp, cwd: Path) -> int:
    """Lance une commande ; copie stdout+stderr ligne par ligne vers console et fp."""
    p = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert p.stdout is not None
    for line in p.stdout:
        sys.stdout.write(line)
        fp.write(line)
        fp.flush()
    return int(p.wait())


def _preflight(fp) -> None:
    _banner("Pré-vol (fichiers / outils)", fp)
    checks: list[tuple[str, bool]] = [
        ("Repo racine", ROOT.is_dir()),
        (str(ROOT / "data" / "active_markets.json"), (ROOT / "data" / "active_markets.json").is_file()),
        (str(ROOT / "data" / "master_stations.sqlite3"), (ROOT / "data" / "master_stations.sqlite3").is_file()),
        (str(ROOT / "data" / "icao_mapping.json"), (ROOT / "data" / "icao_mapping.json").is_file()),
        ("wgrib2 dans PATH", shutil_which("wgrib2") is not None),
    ]
    for label, ok in checks:
        st = "OK" if ok else "MANQUANT ou absent"
        line = f"  [{st}] {label}\n"
        print(line, end="", flush=True)
        fp.write(line)
        fp.flush()


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def main() -> int:
    ap = argparse.ArgumentParser(description="Pipeline NOAA + finance dry + METAR avec logs massifs")
    ap.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Fichier journal (défaut: temp/logs/pipeline_verbose_<UTC>.log)",
    )
    ap.add_argument(
        "--http-debug",
        action="store_true",
        help="Passe --http-debug au producteur NOAA (trace HTTP très verbeux).",
    )
    ap.add_argument("--skip-finance", action="store_true", help="Ne pas lancer finance_engine.")
    ap.add_argument("--skip-bot2", action="store_true", help="Ne pas lancer produce_rt_signals.")
    ap.add_argument("--noaa-only", action="store_true", help="Équivalent --skip-finance --skip-bot2.")
    ap.add_argument(
        "--min-run-interval-sec",
        type=int,
        default=0,
        help="Intervalle min entre runs NOAA (défaut 0 pour tests).",
    )
    args = ap.parse_args()
    if args.noaa_only:
        args.skip_finance = True
        args.skip_bot2 = True

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = args.log_file
    if log_path is None:
        log_dir = ROOT / "temp" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"pipeline_verbose_{ts}.log"

    log_path.parent.mkdir(parents=True, exist_ok=True)

    py = sys.executable
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Chemins bot1 comme update_bot.sh (relatifs racine → absolus)
    for var, rel in (
        ("TRADING_SIGNALS_FILE", "data/trading_signals.json"),
        ("TRADING_SIGNALS_META_FILE", "data/trading_signals_meta.json"),
        ("PORTFOLIO_SQLITE_PATH", "data/portfolio.sqlite"),
        ("NOAA_WATCHDOG_STATE_PATH", "data/logs/noaa_watchdog_state.json"),
        ("LAST_NOAA_RUN_FILE", "data/last_noaa_run.json"),
    ):
        if not (env.get(var) or "").strip():
            env[var] = str((ROOT / rel).resolve())

    exit_code = 0

    with log_path.open("w", encoding="utf-8") as fp:
        head = (
            f"pipeline_verbose démarré {datetime.now(timezone.utc).isoformat()}\n"
            f"python={py}\n"
            f"cwd_repo={ROOT}\n"
            f"log_file={log_path.resolve()}\n"
        )
        print(head, end="")
        fp.write(head)

        _preflight(fp)

        # --- NOAA ---
        _banner("Étape 1/3 — produce_trading_signals.py (NOAA / GFS / wgrib2)", fp)
        noaa_script = ROOT / "bots" / "bot1" / "noaa_nomads_live" / "produce_trading_signals.py"
        cmd = [
            py,
            str(noaa_script),
            "--force",
            "--allow-outside-window",
            "--min-run-interval-sec",
            str(max(0, int(args.min_run_interval_sec))),
        ]
        if args.http_debug:
            cmd.append("--http-debug")

        ec = _stream_subprocess(cmd, env, fp, ROOT)
        if ec != 0:
            exit_code = ec
            fp.write(f"\n[ORCHESTRE] NOAA exit_code={ec}\n")
            print(f"\n[ORCHESTRE] NOAA a échoué (code {ec}) — finance ignorée si dépendante.\n")

        # --- Finance ---
        meta_ok = False
        meta_path = Path(env["TRADING_SIGNALS_META_FILE"])
        if meta_path.is_file():
            try:
                import json

                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta_ok = str(meta.get("status", "")).strip() == "ok"
                fp.write(f"\n[ORCHESTRE] trading_signals_meta status={meta.get('status')!r} ok={meta_ok}\n")
                print(f"\n[ORCHESTRE] meta NOAA status={meta.get('status')!r} ok={meta_ok}\n")
            except Exception as e:
                fp.write(f"\n[ORCHESTRE] lecture meta impossible: {e}\n")
                print(f"\n[ORCHESTRE] meta illisible: {e}\n")

        if not args.skip_finance and meta_ok:
            _banner("Étape 2/3 — finance_engine.py (DRY-RUN forcé)", fp)
            fe_env = env.copy()
            fe_env["FINANCE_ENGINE_DRY_RUN"] = "1"
            fe_env.setdefault("DRY_RUN", "1")
            ec2 = _stream_subprocess([py, str(ROOT / "bots" / "bot1" / "execution" / "finance_engine.py")], fe_env, fp, ROOT)
            if ec2 != 0:
                exit_code = ec2 if exit_code == 0 else exit_code
        elif not args.skip_finance and not meta_ok:
            msg = "[ORCHESTRE] finance_engine sauté (meta NOAA pas ok ou absent)\n"
            fp.write(msg)
            print(msg, end="")

        # --- Bot2 METAR ---
        if not args.skip_bot2:
            _banner("Étape 3/3 — produce_rt_signals (bot2 METAR) — DEBUG logging", fp)

            def _run_bot2() -> None:
                logging.basicConfig(
                    level=logging.DEBUG,
                    format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s — %(message)s",
                    force=True,
                )
                logging.getLogger("httpx").setLevel(logging.DEBUG)
                logging.getLogger("httpcore").setLevel(logging.DEBUG)
                # Import après basicConfig DEBUG : le module bot2 appelle basicConfig(INFO) sans force → no-op
                import importlib

                prt = importlib.import_module("bots.bot2_realtime.metar_live.produce_rt_signals")
                prt.run_realtime_signals()

            try:
                _run_bot2()
            except Exception as e:
                err = f"\n[ORCHESTRE] bot2 exception: {e!r}\n"
                fp.write(err)
                print(err, end="")
                import traceback

                traceback.print_exc(file=fp)
                traceback.print_exc()
                exit_code = exit_code or 1
        else:
            fp.write("\n[ORCHESTRE] bot2 sauté (--skip-bot2 ou --noaa-only)\n")

        tail = f"\n{'='*72}\nFin orchestrateur exit_code={exit_code}\nlog: {log_path.resolve()}\n"
        fp.write(tail)
        print(tail)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
