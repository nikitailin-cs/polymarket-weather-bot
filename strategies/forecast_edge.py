"""
Forecast edge: trade open buckets where the model probability beats the price.

``P(bucket)`` comes from :func:`core.forecast_state.bucket_probability`: a mixture
over the latest run of every model (sigma from ``data/forecast_model_errors.json``),
conditioned on the routine extreme observed so far, collapsing onto the observed
value only when the local day is nearly over *and* observations cover the day.
The intent buys the likelier side with ``p`` = that side's probability; the engine
checks ``p - best_ask >= min_edge`` against the live book.

Parameters (``config/strategies.json`` -> ``forecast_edge``):

- ``enabled``           default false (paper first)
- ``min_edge``          required probability minus best ask (0.08)
- ``max_best_ask``      skip when the best ask is above this (0.99)
- ``max_bet_usdc``      budget per position (25)
- ``max_targets``       most targets per cycle, by confidence (15)
- ``require_full_day``  no intent when observations do not cover the day (true)
- ``min_models``        least complete model runs required (1)
"""
from __future__ import annotations

from core.forecast_state import bucket_probability
from strategies.base import Assessment, Intent, MarketContext, Strategy


class ForecastEdgeStrategy(Strategy):
    """Probabilistic: model-forecast P(bucket) vs the quoted price, only with full-day coverage and a minimum edge."""

    name = "forecast_edge"

    def evaluate(self, ctx: MarketContext) -> Assessment:
        if not ctx.forecasts:
            return Assessment(fields={"p_model": None, "forecast": None})
        bp = bucket_probability(
            variable=ctx.variable,
            low=ctx.bucket.low,
            high=ctx.bucket.high,
            unit=ctx.unit,
            forecasts=ctx.forecasts.values(),
            observed_extreme_c=ctx.extreme_c,
            now_utc=ctx.local_now,
            day_end_utc=ctx.day_end_utc,
            observed_covers_day=ctx.coverage_ok,
        )
        if bp is None:
            return Assessment(fields={"p_model": None, "forecast": None})
        forecast = {"p": bp.p, "models": bp.models, "mean_c": bp.mean_c, "spread_c": bp.spread_c, "note": bp.note, "components": bp.components}
        fields = {"p_model": bp.p, "forecast": forecast}
        if not self.enabled:
            return Assessment(fields=fields)
        if bool(self.params.get("require_full_day", True)) and not ctx.coverage_ok:
            return Assessment(fields={**fields, "forecast_edge_skip": "observations do not cover the day"})
        if bp.models < int(self.params.get("min_models", 1)):
            return Assessment(fields={**fields, "forecast_edge_skip": f"only {bp.models} complete model run(s)"})
        min_edge = float(self.params.get("min_edge", 0.08))
        side = "YES" if bp.p >= 0.5 else "NO"
        conf = bp.p if side == "YES" else 1.0 - bp.p
        if conf < 0.5 + min_edge:
            return Assessment(fields={**fields, "forecast_edge_skip": f"confidence {conf:.2f} below {0.5 + min_edge:.2f}"})
        intent = Intent(
            strategy=self.name,
            side=side,
            p=conf,
            kind="prob",
            min_edge=min_edge,
            max_best_ask=float(self.params.get("max_best_ask", 0.99)),
            max_bet_usdc=self.params.get("max_bet_usdc"),
            reason=f"P({side})={conf:.2f} from {bp.models} model run(s), {bp.note}",
            extra={"models": bp.models, "mean_c": bp.mean_c},
        )
        return Assessment(intent=intent, fields=fields)
