"""
Modèles de données du bot.
Toutes les structures passent par ces dataclasses — jamais de dicts bruts entre modules.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime
from datetime import datetime, timezone


@dataclass
class TokenInfo:
    token_id: str
    outcome: str          # "Yes" / "No"
    price: float          # 0.0 → 1.0 (probabilité implicite du marché)
    winner: Optional[bool] = None


@dataclass
class WeatherMarket:
    condition_id: str
    question: str
    tokens: List[TokenInfo]
    end_date: Optional[datetime]
    volume: float = 0.0
    active: bool = True
    accepting_orders: bool = True
    enable_order_book: bool = True
    matched_keyword: Optional[str] = None

    @property
    def yes_token(self) -> Optional[TokenInfo]:
        for t in self.tokens:
            if t.outcome.lower() == "yes":
                return t
        return None

    @property
    def no_token(self) -> Optional[TokenInfo]:
        for t in self.tokens:
            if t.outcome.lower() == "no":
                return t
        return None

    @property
    def market_yes_prob(self) -> Optional[float]:
        t = self.yes_token
        return t.price if t else None

    @property
    def days_to_expiry(self) -> Optional[float]:
        if self.end_date is None:
            return None
        delta = self.end_date - datetime.now(timezone.utc)
        return delta.total_seconds() / 86400


@dataclass
class WeatherSignal:
    """Probabilité calculée par le moteur météo."""
    market_condition_id: str
    our_yes_prob: float           # Notre estimation P(Yes)
    confidence: float             # 0.0 → 1.0
    sources: List[str] = field(default_factory=list)
    computed_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class TradeSignal:
    """Signal de trading après comparaison P(bot) vs P(marché)."""
    market: WeatherMarket
    signal: WeatherSignal
    side: str                     # "YES" ou "NO"
    edge: float                   # P(bot) - P(marché), positif = avantage
    kelly_stake_usd: float        # Mise recommandée par Kelly
    token_id: str


@dataclass
class Order:
    condition_id: str
    token_id: str
    side: str                     # "BUY" / "SELL"
    size_usd: float
    price: float
    order_id: Optional[str] = None
    status: str = "pending"       # pending / filled / cancelled / failed
    created_at: datetime = field(default_factory=datetime.utcnow)