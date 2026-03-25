#!/usr/bin/env python3
"""
Bot 2 — Moteur financier haute fréquence (aspiration de liquidités sur carnet CLOB).

Phase 4.1 — **Kill-switch de fraîcheur** : manifeste ``realtime_signals_meta.json``, TTL
``REALTIME_SIGNALS_MAX_AGE_SEC``.

Phase 4.2 — **Filtre binaire** : lecture ``realtime_signals.json`` ; seules les cibles
``p_win ∈ {1.0, 0.0}`` avec ``binary_state`` ``met`` / ``dead`` — pas de trade sur 0.5.

Phase 4.3 — **Carnet L2** : même pile proxy HTTP/SOCKS5 que le bot1 (``apply_proxy_env_for_clob`` +
``finance_engine.fetch_clob_book``), tentative ``get_order_book(token_id)`` sur ``ClobClient``,
meilleur ask + filtre ``FINANCE_RT_MAX_BEST_ASK_USDC`` (défaut 0.99 ; ask à 1.00 = refus).

Phase 4.4 — **Sizing** : solde USDC (bridgé + natif) via ``get_usdc_balances`` sur l’adresse
``resolve_clob_funder()`` (**priorité** ``POLYMARKET_PROXY_ADDRESS``), plafond
``FINANCE_RT_MAX_BET_USDC``, parts ``min(budget, liquidité best ask) / prix`` sans dépasser le
reste disponible entre cibles (même logique que bot1 RT).

Phase 4.5 — **Tir** : ordre limite BUY (``token_id``, prix best ask, taille en shares) via
``finance_engine._place_limit_buy`` → ``post_order`` ; avant toute init CLOB :
``apply_rt_signing_env`` (``POLYMARKET_IS_CONTRACT``), ``POLYMARKET_SIGNATURE_TYPE=1``,
``FUNDER_ADDRESS`` aligné sur le proxy si défini ; erreurs CLOB isolées (passage au marché suivant).
Dry-run : ``FINANCE_RT_DRY_RUN`` / ``FINANCE_ENGINE_RT_DRY_RUN`` ou ``DRY_RUN``.

Phase 4.6 — **Registre** : après ordre accepté par le CLOB, insertion dans
``data/bots/bot_2_realtime/portfolio_rt.sqlite``, table ``positions`` (``status=OPEN``), isolée du
bot 1 long-terme.

Phase 4.7 — **Telegram** : après ordre CLOB accepté, ``alert_bot2_rt_scalp_success`` (préfixe
``[SCALP RT]`` / bot2 METAR, distinct du long-terme) ; raison depuis ``realtime_signals.json`` +
``icao_mapping.json``.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

from bots.bot1.execution import finance_engine as fe  # noqa: E402
from core.bot2_data_paths import (  # noqa: E402
    REALTIME_SIGNALS_MAX_AGE_SEC,
    get_rt_paths,
)
from core.bot2_portfolio_schema import (  # noqa: E402
    ensure_bot2_positions_table,
    insert_open_position,
)
from core.bot_rt_env import apply_rt_signing_env  # noqa: E402
from core.client import resolve_clob_funder  # noqa: E402
from core.polymarket_proxy import apply_proxy_env_for_clob  # noqa: E402
from monitoring.error_guard import run_guarded  # noqa: E402
from monitoring.telegram_alert import alert_bot2_rt_scalp_success  # noqa: E402

LOG = logging.getLogger("finance_engine_rt.bot2")

_EPS = 1e-9


def _finance_rt_is_dry_run() -> bool:
    v = (
        os.getenv("FINANCE_RT_DRY_RUN") or os.getenv("FINANCE_ENGINE_RT_DRY_RUN") or ""
    ).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    from config.settings import DRY_RUN  # noqa: WPS433

    return DRY_RUN


def apply_bot2_clob_signing_env() -> None:
    """
    Normalise uniquement les variables RT sans écraser la config du ``.env``.
    L'objectif est d'aligner bot2 RT sur la même config CLOB globale que bot1.
    """
    apply_rt_signing_env()
    proxy = (os.getenv("POLYMARKET_PROXY_ADDRESS") or "").strip()
    if proxy:
        os.environ.setdefault("FUNDER_ADDRESS", proxy)
    sig_type = (os.getenv("POLYMARKET_SIGNATURE_TYPE") or "1").strip() or "1"
    is_contract = (os.getenv("POLYMARKET_IS_CONTRACT") or "").strip().lower()
    LOG.info(
        "bot2 RT: env signature CLOB aligné au .env — POLYMARKET_SIGNATURE_TYPE=%s, "
        "POLYMARKET_IS_CONTRACT=%s",
        sig_type,
        is_contract or "(unset)",
    )


@dataclass(frozen=True)
class BinaryTarget:
    """Cible d’exécution après filtre strict (un marché, un côté outcome)."""

    market_id: str
    outcome_side: str  # "YES" ou "NO"


@dataclass(frozen=True)
class L2BookSnapshot:
    """Meilleur ask côté outcome + validation rentabilité."""

    target: BinaryTarget
    token_id: str
    best_ask: float
    best_ask_size: float
    tick_size: Optional[float]
    passes_profit_cap: bool


@dataclass(frozen=True)
class SizedAttack:
    """Taille d’attaque au best ask (parts / notionnel) après cap solde + ``FINANCE_RT_MAX_BET_USDC``."""

    snapshot: L2BookSnapshot
    funder_address: str
    buying_power_usdc: float
    effective_budget_usdc: float
    shares: float
    notional_usdc: float
    skipped_reason: Optional[str]


def _parse_iso_utc(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _manifest_unix_timestamp(meta: dict[str, Any]) -> Optional[float]:
    """Secondes UNIX UTC du manifeste (priorité ``generated_at_unix`` / ``timestamp``)."""
    for key in ("generated_at_unix", "timestamp"):
        v = meta.get(key)
        if type(v) in (int, float):
            return float(v)
    for key in ("generated_at_utc", "date_utc"):
        dt = _parse_iso_utc(meta.get(key))
        if dt is not None:
            return dt.timestamp()
    return None


def run_antistale_kill_switch() -> int:
    """
    Première action du script : garde-fou manifeste + TTL.

    - Manifeste absent / illisible → ``logger.error``, code 1.
    - ``status != "ok"`` → arrêt immédiat **sans erreur**, code 0.
    - Âge du manifeste ``> REALTIME_SIGNALS_MAX_AGE_SEC`` → ``logger.error`` + code 1.
    - Sinon → code 0.
    """
    paths = get_rt_paths()
    meta_path = paths["meta"]

    if not meta_path.is_file():
        LOG.error("Kill-switch: manifeste introuvable (%s).", meta_path)
        return 1

    try:
        meta: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOG.error("Kill-switch: impossible de lire le manifeste (%s): %s", meta_path, exc)
        return 1

    status = str(meta.get("status", "")).strip()
    if status != "ok":
        LOG.info(
            "Kill-switch: status=%r — trading non autorisé, arrêt propre (exit 0).",
            status,
        )
        return 0

    gen_ts = _manifest_unix_timestamp(meta)
    if gen_ts is None:
        LOG.error(
            "Kill-switch: aucun timestamp exploitable (generated_at_unix, timestamp, "
            "generated_at_utc / date_utc) dans le manifeste."
        )
        return 1

    now = time.time()
    age_sec = now - gen_ts
    max_age = float(REALTIME_SIGNALS_MAX_AGE_SEC)

    if age_sec > max_age:
        LOG.error(
            "Signaux périmés, annulation du run — âge %.1fs > REALTIME_SIGNALS_MAX_AGE_SEC=%.0fs.",
            age_sec,
            max_age,
        )
        return 1

    LOG.info(
        "Kill-switch: OK — manifeste frais (âge %.1fs <= %.0fs, REALTIME_SIGNALS_MAX_AGE_SEC).",
        age_sec,
        max_age,
    )
    return 0


def _load_signals_payload(paths: dict[str, Path]) -> dict[str, Any]:
    sig_path = paths["signals"]
    if not sig_path.is_file():
        raise FileNotFoundError(f"Fichier signaux introuvable: {sig_path}")
    raw = json.loads(sig_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("realtime_signals.json: racine objet attendu")
    return raw


def filter_binary_targets(signals: dict[str, Any]) -> list[BinaryTarget]:
    """
    Ne conserve que les marchés exécutables (pas d’incertitude 0.5).

    - ``p_win == 1.0`` et ``binary_state == "met"`` → achat **YES**.
    - ``p_win == 0.0`` et ``binary_state == "dead"`` → achat **NO**.
    """
    out: list[BinaryTarget] = []
    for market_id, row in signals.items():
        if not isinstance(market_id, str) or not market_id.strip():
            continue
        if market_id.startswith("_"):
            continue
        if not isinstance(row, dict):
            continue
        try:
            p_win = float(row.get("p_win"))
        except (TypeError, ValueError):
            continue
        bs = str(row.get("binary_state", "")).strip().lower()

        if abs(p_win - 0.5) < _EPS:
            continue

        if abs(p_win - 1.0) < _EPS and bs == "met":
            out.append(BinaryTarget(market_id=market_id.strip(), outcome_side="YES"))
            continue
        if abs(p_win - 0.0) < _EPS and bs == "dead":
            out.append(BinaryTarget(market_id=market_id.strip(), outcome_side="NO"))
            continue

    return out


def _mapping_by_market_id(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
    """Lit ``icao_mapping.json`` : ``market_id`` → config (icao, name, …)."""
    mp = paths.get("mapping")
    if not mp or not mp.is_file():
        return {}
    try:
        raw: Any = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for mid, cfg in raw.items():
        if isinstance(mid, str) and isinstance(cfg, dict):
            out[mid.strip()] = cfg
    return out


def _bot2_rt_telegram_reason(
    signal_row: dict[str, Any],
    outcome_side: str,
    map_cfg: dict[str, Any],
) -> str:
    """Texte « Raison : … » pour l’alerte (aligné METAR + marché)."""
    name = str(map_cfg.get("name", "")).strip()
    unit = str(map_cfg.get("threshold_unit", "F") or "F").upper()
    udisp = "°F" if unit.startswith("F") else "°C"
    try:
        temp = float(signal_row.get("current_obs_temp"))
        temp_s = f"{temp:.1f}"
    except (TypeError, ValueError):
        temp_s = "—"
    ou = (outcome_side or "").upper()
    if ou == "YES":
        if name:
            return f"{name} — observation {temp_s}{udisp}, validée par METAR."
        return f"Seuil atteint — observation {temp_s}{udisp} (METAR)."
    if ou == "NO":
        if name:
            return f"Position NO (marché mort) — {name}. METAR."
        return "Signal NO — marché mort (METAR)."
    return "Scalping RT (METAR)."


def _finance_rt_max_best_ask_usdc() -> float:
    try:
        return float(os.getenv("FINANCE_RT_MAX_BEST_ASK_USDC", "0.99"))
    except ValueError:
        return 0.99


def _finance_rt_max_bet_usdc() -> float:
    try:
        return float(os.getenv("FINANCE_RT_MAX_BET_USDC", "50.0"))
    except ValueError:
        return 50.0


def _finance_rt_usdc_reserve() -> float:
    try:
        return float(os.getenv("FINANCE_RT_USDC_RESERVE", "0") or "0")
    except ValueError:
        return 0.0


def _finance_rt_min_order_notional_usdc() -> float:
    try:
        return float(os.getenv("FINANCE_RT_MIN_ORDER_NOTIONAL_USDC", "1.0"))
    except ValueError:
        return 1.0


def fetch_buying_power_usdc_for_proxy() -> tuple[float, Optional[str]]:
    """
    USDC disponibles pour ordres : solde on-chain (USDC.e + natif) sur l’adresse **funder**
    (``resolve_clob_funder`` → en priorité ``POLYMARKET_PROXY_ADDRESS``), moins
    ``FINANCE_RT_USDC_RESERVE``.

    Retourne ``(pouvoir, adresse_affichée)`` ; ``math.inf`` si lecture impossible (sizing plafonné
    par ``FINANCE_RT_MAX_BET_USDC`` seul).
    """
    from execution.ctf_manager import get_usdc_balances  # noqa: WPS433

    addr = resolve_clob_funder()
    if not addr:
        LOG.warning(
            "Sizing 4.4: aucune adresse funder (POLYMARKET_PROXY_ADDRESS, POLYMARKET_FUNDER, …) — "
            "budget plafonné par FINANCE_RT_MAX_BET_USDC uniquement."
        )
        return math.inf, None
    try:
        b = get_usdc_balances(addr)
        total = float(b.get("total", 0.0))
        reserve = _finance_rt_usdc_reserve()
        out = max(0.0, total - reserve)
        LOG.info(
            "Sizing 4.4: USDC proxy/funder=%s… total=%.4f (réserve %.4f) → %.4f disponible",
            addr[:10],
            total,
            reserve,
            out,
        )
        return out, addr
    except Exception as exc:
        LOG.warning("Sizing 4.4: lecture USDC impossible (%s) — cap MAX_BET seul", exc)
        return math.inf, addr


def _token_id_for_outcome(market_id: str, outcome_side: str) -> Optional[str]:
    pair = fe._resolve_tokens_for_market(market_id)
    if pair is None:
        return None
    if outcome_side.upper() == "YES":
        return pair.yes_token_id
    return pair.no_token_id


def fetch_l2_orderbook(token_id: str) -> Optional[dict[str, Any]]:
    """
    Carnet L2 pour ``token_id`` : tentative ``get_order_book`` / ``get_orderbook`` sur
    ``ClobClient`` (même init que le bot1), sinon REST ``/book`` via ``fetch_clob_book``
    (``bot1_http`` → SOCKS5 si configuré).
    """
    apply_proxy_env_for_clob()
    try:
        client = fe._get_clob_client()
        for name in ("get_order_book", "get_orderbook"):
            fn = getattr(client, name, None)
            if not callable(fn):
                continue
            try:
                ob = fn(token_id)
            except Exception:
                continue
            if isinstance(ob, dict) and (
                ob.get("asks") is not None
                or ob.get("sell") is not None
                or ob.get("bids") is not None
            ):
                return ob
    except Exception as exc:
        LOG.debug("CLOB client / get_order_book indisponible (%s) — fallback REST /book", exc)

    return fe.fetch_clob_book(token_id)


def analyze_l2_for_targets(targets: list[BinaryTarget]) -> list[L2BookSnapshot]:
    """
    Pour chaque cible : meilleur ask (prix vendeur le plus bas) sur le token YES/NO.
    Filtre ``FINANCE_RT_MAX_BEST_ASK_USDC`` : best ask > plafond ou >= 1.00 → ``passes_profit_cap=False``.
    """
    cap = _finance_rt_max_best_ask_usdc()
    out: list[L2BookSnapshot] = []

    for t in targets:
        tid = _token_id_for_outcome(t.market_id, t.outcome_side)
        if not tid:
            LOG.warning(
                "L2: token_ids introuvables market_id=%s…",
                t.market_id[:24] + "…" if len(t.market_id) > 28 else t.market_id,
            )
            continue

        book = fetch_l2_orderbook(tid)
        if not book:
            LOG.warning(
                "L2: carnet vide ou indisponible outcome=%s token=%s…",
                t.outcome_side,
                tid[:16],
            )
            continue

        top = fe._extract_best_ask_from_book(book)
        if top is None:
            LOG.warning(
                "L2: pas d’asks pour outcome=%s market=%s…",
                t.outcome_side,
                t.market_id[:20],
            )
            continue

        best = fe._normalize_price_to_tick(top.best_ask, top.tick_size)
        if best >= 1.0 - 1e-12:
            LOG.info(
                "L2: SKIP — best ask=%.4f (≈1.00, aucun profit) market=%s…",
                best,
                t.market_id[:20],
            )
            out.append(
                L2BookSnapshot(
                    target=t,
                    token_id=tid,
                    best_ask=best,
                    best_ask_size=top.best_ask_size,
                    tick_size=top.tick_size,
                    passes_profit_cap=False,
                )
            )
            continue

        ok = best <= cap + 1e-9
        if not ok:
            LOG.info(
                "L2: SKIP rentabilité — best ask=%.4f > plafond FINANCE_RT_MAX_BEST_ASK_USDC=%.4f "
                "market=%s…",
                best,
                cap,
                t.market_id[:20],
            )
        else:
            LOG.info(
                "L2: OK — best ask=%.4f <= %.4f (outcome=%s, size=%.4f) market=%s…",
                best,
                cap,
                t.outcome_side,
                top.best_ask_size,
                t.market_id[:20],
            )

        out.append(
            L2BookSnapshot(
                target=t,
                token_id=tid,
                best_ask=best,
                best_ask_size=top.best_ask_size,
                tick_size=top.tick_size,
                passes_profit_cap=ok,
            )
        )

    return out


def size_attacks_from_l2(
    snapshots: list[L2BookSnapshot],
) -> list[SizedAttack]:
    """
    « All-in sécurisé » : pour chaque snapshot avec ``passes_profit_cap``, budget effectif =
    ``min(FINANCE_RT_MAX_BET_USDC, solde_restant)``, puis parts = budget / best ask, plafonnées par
    la liquidité au best ask.
    """
    max_bet = _finance_rt_max_bet_usdc()
    min_notional = _finance_rt_min_order_notional_usdc()
    buying_power, funder = fetch_buying_power_usdc_for_proxy()
    addr_display = funder or ""

    remaining = buying_power
    out: list[SizedAttack] = []

    for snap in snapshots:
        if not snap.passes_profit_cap:
            out.append(
                SizedAttack(
                    snapshot=snap,
                    funder_address=addr_display,
                    buying_power_usdc=buying_power,
                    effective_budget_usdc=0.0,
                    shares=0.0,
                    notional_usdc=0.0,
                    skipped_reason="L2 non rentable (profit cap)",
                )
            )
            continue

        price = snap.best_ask
        if price <= _EPS:
            out.append(
                SizedAttack(
                    snapshot=snap,
                    funder_address=addr_display,
                    buying_power_usdc=buying_power,
                    effective_budget_usdc=0.0,
                    shares=0.0,
                    notional_usdc=0.0,
                    skipped_reason="best ask nul ou invalide",
                )
            )
            continue

        if remaining is not math.inf:
            eff_budget = min(max_bet, max(0.0, remaining))
        else:
            eff_budget = max_bet

        size_shares = eff_budget / price
        if snap.best_ask_size > 0:
            size_shares = min(size_shares, snap.best_ask_size)
        if remaining is not math.inf:
            max_sh_from_cash = remaining / price
            size_shares = min(size_shares, max_sh_from_cash)

        notional = size_shares * price
        if notional + _EPS < min_notional:
            out.append(
                SizedAttack(
                    snapshot=snap,
                    funder_address=addr_display,
                    buying_power_usdc=buying_power,
                    effective_budget_usdc=eff_budget,
                    shares=0.0,
                    notional_usdc=notional,
                    skipped_reason=f"notionnel {notional:.4f} < FINANCE_RT_MIN_ORDER_NOTIONAL_USDC={min_notional}",
                )
            )
            continue

        if remaining is not math.inf:
            remaining = max(0.0, remaining - notional)

        LOG.info(
            "Sizing 4.4: market=%s… shares=%.6f ask=%.4f notionnel~%.4f USDC (budget eff.=%.4f, max_bet=%.4f)",
            snap.target.market_id[:20],
            size_shares,
            price,
            notional,
            eff_budget,
            max_bet,
        )

        out.append(
            SizedAttack(
                snapshot=snap,
                funder_address=addr_display,
                buying_power_usdc=buying_power,
                effective_budget_usdc=eff_budget,
                shares=size_shares,
                notional_usdc=notional,
                skipped_reason=None,
            )
        )

    return out


def execute_limit_orders_phase45(
    sized: list[SizedAttack],
    portfolio_db_path: Path,
    signals: dict[str, Any],
    paths: dict[str, Path],
) -> tuple[int, int]:
    """
    Pour chaque ligne sized valide : ``_place_limit_buy`` (LIMIT BUY, side implicite BUY).
    Échecs isolés (log warning, pas d’exception propagée).
    Ordre accepté CLOB → ligne ``positions`` (``OPEN``) dans ``portfolio_db_path`` ;
    alerte Telegram 4.7 (``signals`` + mapping ICAO).
    Retourne ``(acceptés_CLOB, échecs_ou_rejets)``.
    """
    apply_proxy_env_for_clob()
    ok_n = 0
    fail_n = 0
    mapping = _mapping_by_market_id(paths)

    portfolio_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(portfolio_db_path))
    try:
        ensure_bot2_positions_table(conn)
    except Exception as exc:
        conn.close()
        LOG.error("4.6: impossible d’initialiser la base %s — %s", portfolio_db_path, exc)
        return 0, 0

    try:
        for row in sized:
            if row.skipped_reason is not None or row.shares <= _EPS:
                continue
            snap = row.snapshot
            tid = snap.token_id
            price = snap.best_ask
            size = row.shares

            try:
                resp = fe._place_limit_buy(token_id=tid, price=price, size=size)
            except Exception as exc:
                fail_n += 1
                LOG.warning(
                    "4.5: exception ordre market=%s… outcome=%s token=%s… — %s",
                    snap.target.market_id[:20],
                    snap.target.outcome_side,
                    tid[:16],
                    exc,
                )
                continue

            accepted, reason = fe._clob_order_accepted(resp)
            if not accepted:
                fail_n += 1
                LOG.warning(
                    "4.5: CLOB rejet / pas d’orderID market=%s… outcome=%s — %s réponse=%s",
                    snap.target.market_id[:20],
                    snap.target.outcome_side,
                    reason,
                    str(resp)[:800],
                )
                continue

            ok_n += 1
            oid = resp.get("orderID") or resp.get("order_id") or resp.get("id")
            LOG.info(
                "4.5: ordre accepté market=%s… outcome=%s orderID=%s notionnel~%.4f USDC",
                snap.target.market_id[:20],
                snap.target.outcome_side,
                oid,
                row.notional_usdc,
            )

            sig_row = signals.get(snap.target.market_id)
            if not isinstance(sig_row, dict):
                sig_row = {}
            map_cfg = mapping.get(snap.target.market_id)
            if not isinstance(map_cfg, dict):
                map_cfg = {}
            icao = str(map_cfg.get("icao", "") or "").strip().upper() or "—"
            reason = _bot2_rt_telegram_reason(sig_row, snap.target.outcome_side, map_cfg)
            try:
                alert_bot2_rt_scalp_success(
                    station_icao=icao,
                    shares=size,
                    outcome_side=snap.target.outcome_side,
                    price=price,
                    reason_line=reason,
                    stake_usdc=row.notional_usdc,
                    market_id=snap.target.market_id,
                    order_id=str(oid or ""),
                )
            except Exception:
                LOG.debug("4.7: Telegram bot2 ignorée", exc_info=True)

            ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            try:
                insert_open_position(
                    conn,
                    market_id=snap.target.market_id,
                    side=snap.target.outcome_side,
                    price=price,
                    size=size,
                    timestamp_iso_utc=ts,
                )
                conn.commit()
                LOG.info(
                    "4.6: positions OPEN enregistrée market=%s… side=%s price=%.6f size=%.6f",
                    snap.target.market_id[:20],
                    snap.target.outcome_side,
                    price,
                    size,
                )
            except Exception as exc:
                LOG.warning(
                    "4.6: échec persistance SQLite (ordre tout de même accepté CLOB) — %s",
                    exc,
                )
    finally:
        conn.close()

    return ok_n, fail_n


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    load_dotenv(dotenv_path=_ROOT / ".env", override=False)
    apply_bot2_clob_signing_env()
    apply_proxy_env_for_clob()

    rc = run_antistale_kill_switch()
    if rc != 0:
        return rc

    paths = get_rt_paths()
    try:
        signals = _load_signals_payload(paths)
    except Exception as exc:
        LOG.error("Chargement signaux: %s", exc)
        return 1

    targets = filter_binary_targets(signals)
    if not targets:
        LOG.info(
            "Filtre binaire: aucune cible (p_win 1.0/met ou 0.0/dead) — rien à exécuter."
        )
        return 0

    for t in targets:
        LOG.info(
            "Cible binaire: market_id=%s… outcome=%s",
            t.market_id[:24] + "…" if len(t.market_id) > 28 else t.market_id,
            t.outcome_side,
        )
    LOG.info("Filtre binaire: %s marché(s) — analyse L2 (CLOB).", len(targets))

    snapshots = analyze_l2_for_targets(targets)
    n_ok = sum(1 for s in snapshots if s.passes_profit_cap)
    LOG.info(
        "L2: %s snapshot(s), %s avec best ask acceptable.",
        len(snapshots),
        n_ok,
    )

    sized = size_attacks_from_l2(snapshots)
    n_placed = sum(1 for x in sized if x.skipped_reason is None and x.shares > _EPS)
    LOG.info(
        "Sizing 4.4: %s ligne(s), %s avec taille > 0.",
        len(sized),
        n_placed,
    )

    dry = _finance_rt_is_dry_run()
    if dry:
        LOG.info(
            "4.5: mode DRY-RUN — aucun post_order (FINANCE_RT_DRY_RUN / DRY_RUN)."
        )
        for row in sized:
            if row.skipped_reason is not None or row.shares <= _EPS:
                continue
            s = row.snapshot
            LOG.info(
                "[DRY-RUN 4.5] LIMIT BUY token=%s… price=%.6f size=%.6f outcome=%s notionnel~%.4f",
                s.token_id[:16],
                s.best_ask,
                row.shares,
                s.target.outcome_side,
                row.notional_usdc,
            )
    else:
        ok45, fail45 = execute_limit_orders_phase45(sized, paths["db"], signals, paths)
        LOG.info(
            "4.5: exécution terminée — CLOB acceptés=%s échecs/rejets=%s",
            ok45,
            fail45,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(run_guarded("bot2.finance_engine_rt", main))
