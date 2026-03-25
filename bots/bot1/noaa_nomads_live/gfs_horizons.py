"""
Horizons GFS (fXXX) alignés sur le jour calendaire du contrat (timezone lieu).

Convention : *valid time* = ``run_start_utc + timedelta(hours=fh)`` (UTC), puis filtrage
des ``fh`` dont l'instant tombe dans ``[target_date 00:00, target_date+1 00:00)`` en
heure locale (IANA).

Pour obtenir ``run_start_utc`` depuis un ``GfsRun`` : même règle que
``noaa_engine._cycle_to_datetime(run.day, run.cycle_utc)`` — utiliser
:func:`gfs_run_start_utc_from_parts`.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "calculate_gfs_horizons",
    "gfs_run_start_utc_from_parts",
    "sigma_horizon_multiplier",
]

_GFS_CYCLES = frozenset({"00", "06", "12", "18"})


def gfs_run_start_utc_from_parts(day: date, cycle_utc: str) -> datetime:
    """
    Instant UTC de départ de la run GFS (00 / 06 / 12 / 18 Z).

    Aligné sur ``noaa_engine._cycle_to_datetime`` (NOMADS).
    """
    c = (cycle_utc or "00").strip().zfill(2)
    if c not in _GFS_CYCLES:
        raise ValueError(f"Cycle GFS invalide : {cycle_utc!r} (attendu 00, 06, 12 ou 18)")
    hh = int(c)
    return datetime(day.year, day.month, day.day, hh, 0, 0, tzinfo=timezone.utc)


def calculate_gfs_horizons(
    run_start_utc: datetime,
    target_date: date,
    locality_tz: str,
    *,
    step_hours: int = 3,
    max_fh: int = 384,
) -> tuple[int, ...]:
    """
    Retourne les échéances GFS ``fh`` (pas de ``step_hours``) couvrant la journée locale
    ``target_date`` dans ``locality_tz``.

    - **Temps zéro** : ``run_start_utc`` (instant UTC de la run, ex. 24 mars 12:00 UTC).
    - **Cible** : ``[target_date 00:00, lendemain 00:00)`` en heure locale.
    - **Delta** : pour chaque ``fh``, ``valid_utc = run_start_utc + fh h`` ; on garde ``fh``
      si l'instant local correspond à ``target_date``.

    Exemple (UTC, run 2026-03-24 12Z, cible 2026-03-27) ::
        (60, 63, 66, 69, 72, 75, 78, 81)

    Pour Paris ou autres IANA, la liste dépend du fuseau et du DST.
    """
    if step_hours <= 0:
        raise ValueError("step_hours doit être > 0")
    if max_fh < 0:
        raise ValueError("max_fh doit être >= 0")

    if run_start_utc.tzinfo is None:
        run_utc = run_start_utc.replace(tzinfo=timezone.utc)
    else:
        run_utc = run_start_utc.astimezone(timezone.utc)

    try:
        tz = ZoneInfo((locality_tz or "UTC").strip() or "UTC")
    except ZoneInfoNotFoundError as e:
        raise ValueError(f"Fuseau IANA invalide: {locality_tz!r}") from e

    day_start = datetime.combine(target_date, time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    out: list[int] = []
    for fh in range(0, max_fh + 1, step_hours):
        valid_utc = run_utc + timedelta(hours=fh)
        local = valid_utc.astimezone(tz)
        if day_start <= local < day_end:
            out.append(fh)
    return tuple(out)


def sigma_horizon_multiplier(
    horizon_hours: float,
    *,
    ref_hours: float = 24.0,
    slope: float = 0.5,
) -> float:
    """
    Facteur ≥ 1 appliqué à σ (Gaussienne ``p_win``) : plus l’horizon GFS est lointain,
    plus l’incertitude augmente (courbe plus plate).

    - ``horizon_hours`` : en pratique ``max(fh)`` utilisé pour le marché.
    - Au-delà de ``ref_hours``, ``mult = 1 + slope * (horizon_hours - ref_hours) / 24``.

    Défauts : ``ref_hours=24``, ``slope=0.5`` → f120 h donne mult ≈ 3.
    """
    if horizon_hours <= 0:
        return 1.0
    if horizon_hours <= ref_hours:
        return 1.0
    return 1.0 + slope * ((horizon_hours - ref_hours) / 24.0)
