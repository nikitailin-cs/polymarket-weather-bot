"""
Sniper: trade a bucket the routine extreme has already decided.

The day's max can only rise and the min can only fall, so once the resolution
value (routine METARs, tenths, market rounding) has moved past a bucket that
bucket is dead (buy NO) and once it has entered an open-ended bucket, or the
day is over inside a bucket, the bucket is met (buy YES). Nothing probabilistic:
``p`` is 1.0 for the bought side; the only risks are the resolution source
disagreeing with our observations and the price already being at 1.00.

Parameters (``config/strategies.json`` -> ``sniper``):

- ``enabled``       default true
- ``max_best_ask``  skip when the best ask is above this (0.99)
- ``max_bet_usdc``  budget per position (50)
- ``max_targets``   most targets per cycle, YES first (40)
"""
from __future__ import annotations

from core.rt_daily_extrema import decide_bucket
from strategies.base import Assessment, Intent, MarketContext, Strategy


class SniperStrategy(Strategy):
    name = "sniper"

    def evaluate(self, ctx: MarketContext) -> Assessment:
        if not ctx.has_day_state:
            return Assessment(fields={"binary_state": "open", "p_win": 0.5, "decision_reason": "no day state yet"})
        d = decide_bucket(
            variable=ctx.variable,
            bucket=ctx.bucket,
            extreme_value=float(ctx.resolution_value),
            local_now=ctx.local_now,
            deadline_hhmm=ctx.deadline_hhmm,
            dead_min_gap=ctx.dead_min_gap,
        )
        fields = {"binary_state": d.binary_state, "p_win": d.p_win, "decision_reason": d.reason}
        if not self.enabled or d.binary_state == "open":
            return Assessment(fields=fields)
        side = "YES" if d.binary_state == "met" else "NO"
        intent = Intent(
            strategy=self.name,
            side=side,
            p=1.0,
            kind="binary",
            min_edge=0.0,
            max_best_ask=float(self.params.get("max_best_ask", 0.99)),
            max_bet_usdc=self.params.get("max_bet_usdc"),
            reason=d.reason,
        )
        return Assessment(intent=intent, fields=fields)
