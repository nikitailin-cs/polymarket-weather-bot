"""
Trading strategies, each in its own file, all behind one contract.

The producer builds a :class:`MarketContext` per market (facts only: bucket,
observed extreme, forecasts, time left) and asks every enabled strategy for an
:class:`Intent`. The finance engine executes intents (order book, edge, sizing,
paper or live) and decides nothing by itself.

- :mod:`strategies.sniper`         binary: a bucket already decided by the routine extreme
- :mod:`strategies.forecast_edge`  probabilistic: model forecasts vs the quoted price

Parameters live in ``config/strategies.json`` (``STRATEGIES_CONFIG`` to override);
the historical environment variables still apply on top.
"""
from __future__ import annotations

from strategies.base import Intent, MarketContext, Strategy
from strategies.config import StrategyConfig, load_config, load_overrides, overrides_path, set_override
from strategies.registry import REGISTRY, load_strategies

__all__ = ["Intent", "MarketContext", "REGISTRY", "Strategy", "StrategyConfig", "load_config", "load_overrides", "load_strategies", "overrides_path", "set_override"]
