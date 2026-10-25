"""Strategy contract: facts in (:class:`MarketContext`), intent out (:class:`Intent`)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from core.forecast_state import ForecastRecord
from core.rt_daily_extrema import Bucket


@dataclass(frozen=True)
class MarketContext:
    """Everything a strategy may look at for one market. Built by the producer, no I/O inside strategies."""

    market_id: str
    icao: str
    variable: str  # temp_max | temp_min
    unit: str  # F | C
    bucket: Bucket
    target_date: str  # YYYY-MM-DD, station local day
    timezone: str
    local_now: datetime  # aware, station zone
    deadline_hhmm: str = "23:59"
    dead_min_gap: float = 15.0
    day_end_utc: Optional[datetime] = None
    # observed routine extreme of the local day (None when the day state does not exist yet)
    extreme_c: Optional[float] = None
    resolution_value: Optional[int] = None  # whole degrees in the market unit, market rounding rule
    coverage_ok: bool = False  # observations cover the day since local midnight
    obs_count: int = 0
    lead_value_c: Optional[float] = None  # incl. SPECI / obs10, informational
    forecasts: dict[str, ForecastRecord] = field(default_factory=dict)

    @property
    def has_day_state(self) -> bool:
        return self.extreme_c is not None and self.resolution_value is not None


@dataclass(frozen=True)
class Intent:
    """What a strategy wants: buy ``side`` with estimated probability ``p`` of that side winning."""

    strategy: str
    side: str  # YES | NO
    p: float  # probability that the bought side wins, 0..1
    kind: str  # binary | prob
    min_edge: float = 0.0  # required p - best_ask
    max_best_ask: float = 0.99
    max_bet_usdc: Optional[float] = None  # None = engine default
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "side": self.side,
            "p": round(self.p, 4),
            "kind": self.kind,
            "min_edge": self.min_edge,
            "max_best_ask": self.max_best_ask,
            "max_bet_usdc": self.max_bet_usdc,
            "reason": self.reason,
            **({"extra": self.extra} if self.extra else {}),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Optional["Intent"]:
        try:
            return cls(
                strategy=str(d["strategy"]),
                side=str(d["side"]).upper(),
                p=float(d["p"]),
                kind=str(d.get("kind") or "prob"),
                min_edge=float(d.get("min_edge") or 0.0),
                max_best_ask=float(d.get("max_best_ask") or 0.99),
                max_bet_usdc=float(d["max_bet_usdc"]) if d.get("max_bet_usdc") is not None else None,
                reason=str(d.get("reason") or ""),
                extra=dict(d.get("extra") or {}),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class Assessment:
    """A strategy's full view of a market: the intent (if any) plus fields for the signal row."""

    intent: Optional[Intent] = None
    fields: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    name: str = "strategy"

    def __init__(self, params: Optional[dict[str, Any]] = None) -> None:
        self.params: dict[str, Any] = dict(params or {})

    @property
    def enabled(self) -> bool:
        return bool(self.params.get("enabled", True))

    @abstractmethod
    def evaluate(self, ctx: MarketContext) -> Assessment:
        """Look at the facts, return an assessment (intent may be None)."""

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "enabled": self.enabled, "params": self.params}
