"""Strategy registry: name -> class, and the loader that applies the config."""
from __future__ import annotations

from typing import Optional

from strategies.base import Strategy
from strategies.config import StrategyConfig, load_config
from strategies.forecast_edge import ForecastEdgeStrategy
from strategies.sniper import SniperStrategy

REGISTRY: dict[str, type[Strategy]] = {
    SniperStrategy.name: SniperStrategy,
    ForecastEdgeStrategy.name: ForecastEdgeStrategy,
}


def load_strategies(config: Optional[StrategyConfig] = None, *, include_disabled: bool = True) -> list[Strategy]:
    """All known strategies with their parameters; disabled ones still produce signal fields."""
    cfg = config or load_config()
    out: list[Strategy] = []
    for name, cls in REGISTRY.items():
        s = cls(cfg.params(name))
        if include_disabled or s.enabled:
            out.append(s)
    return out
