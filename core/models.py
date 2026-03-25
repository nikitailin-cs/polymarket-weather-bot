"""
Modèles de données du bot.
Toutes les structures passent par ces dataclasses — jamais de dicts bruts entre modules.

v2 : ajout ContractSpec + MarketStatus pour la couche contractuelle.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional, List, Tuple
from datetime import datetime, timezone
from enum import Enum
import json
import os
import threading
import tempfile


# ══════════════════════════════════════════════════════════════════════════════
# Modèles existants (inchangés)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TokenInfo:
    token_id: str
    outcome:  str           # "Yes" / "No"
    price:    float         # 0.0 → 1.0 (probabilité implicite du marché)
    winner:   Optional[bool] = None


@dataclass
class WeatherMarket:
    condition_id:      str
    question:          str
    tokens:            List[TokenInfo]
    end_date:          Optional[datetime]
    volume:            float = 0.0
    active:            bool  = True
    accepting_orders:  bool  = True
    enable_order_book: bool  = True
    matched_keyword:   Optional[str] = None
    # Champs Gamma (remplis par le scanner pour generate_markets / audit)
    liquidity_usd:     float = 0.0
    resolution_source: str = ""
    end_date_iso_raw:  str = ""
    slug:              str = ""
    gamma_id:          str = ""
    gamma_raw:         dict[str, Any] = field(default_factory=dict)

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
    our_yes_prob:        float
    confidence:          float
    sources:             List[str]         = field(default_factory=list)
    computed_at:         datetime          = field(default_factory=datetime.utcnow)


@dataclass
class TradeSignal:
    """Signal de trading après comparaison P(bot) vs P(marché)."""
    market:          WeatherMarket
    signal:          WeatherSignal
    side:            str    # "YES" ou "NO"
    edge:            float
    kelly_stake_usd: float
    token_id:        str
    # Stratégie haltère (``core.barbell_strategy``) — None si désactivée ou hors jambe.
    barbell_leg:     Optional[str] = None


@dataclass
class Order:
    condition_id: str
    token_id:     str
    side:         str       # "BUY" / "SELL"
    size_usd:     float
    price:        float
    order_id:     Optional[str] = None
    status:       str            = "pending"
    created_at:   datetime       = field(default_factory=datetime.utcnow)


# ══════════════════════════════════════════════════════════════════════════════
# Nouveaux modèles contractuels (v2)
# ══════════════════════════════════════════════════════════════════════════════

class MarketStatus(str, Enum):
    """Cycle de vie complet d'un marché Polymarket."""
    ACTIVE          = "ACTIVE"          # trading ouvert
    CLOSED_PENDING  = "CLOSED_PENDING"  # fermé, oracle pas encore tranché
    RESOLVED_YES    = "RESOLVED_YES"    # share YES → 1.00 $
    RESOLVED_NO     = "RESOLVED_NO"     # share YES → 0.00 $
    CANCELLED       = "CANCELLED"       # litige UMA / remboursement partiel


@dataclass
class ContractSpec:
    """
    Spécification contractuelle d'un marché météo.
    Créée UNE SEULE FOIS par marché (parsing), mise en cache JSON, relue à chaque cycle.
    """
    # Identifiants
    market_id:    str = ""
    condition_id: str = ""
    question:     str = ""

    # Géographie
    city:    str            = ""
    country: str            = ""

    # Parsing contractuel
    weather_variable: str   = "unknown"   # temp_max / temp_min / precip / snow / wind_avg
    threshold_value:  float = 0.0
    threshold_unit:   str   = "unknown"   # celsius / fahrenheit / mm / cm / kmh
    threshold_op:     str   = "gte"       # gt / gte / lt / lte / eq / between
    threshold_range:  Optional[List[float]] = None  # [low, high] si op=between
    time_window_utc:  Optional[List[int]] = None   # [heure_debut, heure_fin]

    # Source oracle
    oracle_station_id: str  = ""          # ex: "KNYC", "07156"
    oracle_source:     str  = "unknown"   # noaa_ghcnd / wmo_station / manual
    oracle_rounding:   int  = 1

    # Confiance alignement modèle ↔ oracle  (0.0 → 1.0)
    oracle_alignment_score: float = 0.0

    # Dates
    end_date_iso: str = ""

    # Source de résolution décentralisée
    # "uma"     → Optimistic Oracle UMA (quasi-tous les marchés météo Polymarket)
    #             Mécanisme : proposer → fenêtre dispute 2h → résolution auto ou vote (7-14j)
    # "pyth"    → Oracle on-chain temps réel (marchés financiers)
    # "manual"  → Résolution manuelle par Polymarket Labs
    # "unknown" → Non déterminé au parsing
    resolution_source: str = "unknown"

    # Fees
    fee_bps:          int = 0
    maker_rebate_bps: int = 0

    # État (mutable)
    status:              MarketStatus    = MarketStatus.ACTIVE
    last_oracle_poll:    Optional[str]   = None
    resolution_price:    Optional[float] = None
    cancellation_reason: Optional[str]   = None

    # ── Propriétés ────────────────────────────────────────────────────────────

    @property
    def is_tradeable(self) -> bool:
        return self.status == MarketStatus.ACTIVE

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            MarketStatus.RESOLVED_YES,
            MarketStatus.RESOLVED_NO,
            MarketStatus.CANCELLED,
        )

    @property
    def effective_edge_multiplier(self) -> float:
        """Facteur de déflation de l'edge brut selon la confiance oracle."""
        if not self.is_tradeable:
            return 0.0
        return max(0.0, self.oracle_alignment_score)

    def evaluate(self, observed_value: float) -> bool:
        """Vérifie si la valeur observée satisfait la condition contractuelle."""
        v = round(observed_value, self.oracle_rounding)
        if self.threshold_op == "between":
            if not self.threshold_range or len(self.threshold_range) != 2:
                return False
            low, high = self.threshold_range
            if low > high:
                low, high = high, low
            return low <= v <= high
        t = self.threshold_value
        return {
            "gt":  v > t,
            "gte": v >= t,
            "lt":  v < t,
            "lte": v <= t,
            "eq":  v == t,
        }.get(self.threshold_op, False)

    # ── Sérialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        d = {}
        for f_name in self.__dataclass_fields__:
            val = getattr(self, f_name)
            if isinstance(val, MarketStatus):
                val = val.value
            d[f_name] = val
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ContractSpec":
        d = dict(d)
        if "status" in d:
            try:
                d["status"] = MarketStatus(d["status"])
            except ValueError:
                d["status"] = MarketStatus.ACTIVE
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ══════════════════════════════════════════════════════════════════════════════
# Cache JSON thread-safe pour ContractSpec
# ══════════════════════════════════════════════════════════════════════════════

class ContractSpecCache:
    """
    Lit/écrit les ContractSpec sur disque (JSON).
    - Lecture : O(1) depuis dict mémoire, 0 I/O disque en boucle chaude
    - Écriture : atomique (tmp + rename), tout dans le même RLock
    """

    def __init__(self, path: str):
        from pathlib import Path
        self._path  = Path(path)
        self._lock  = threading.RLock()
        self._specs: dict[str, ContractSpec] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            with self._lock:
                self._specs = {mid: ContractSpec.from_dict(d) for mid, d in raw.items()}
        except Exception:
            self._specs = {}

    def _save(self) -> None:
        """Écriture atomique — tout dans le même lock pour éviter la race condition."""
        with self._lock:
            data = {mid: spec.to_dict() for mid, spec in self._specs.items()}
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # IMPORTANT: nom temporaire unique pour éviter collisions inter-processus.
            fd, tmp_name = tempfile.mkstemp(
                prefix=f"{self._path.stem}.",
                suffix=".tmp",
                dir=str(self._path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(data, indent=2, ensure_ascii=False))
                os.replace(tmp_name, str(self._path))
            finally:
                try:
                    if os.path.exists(tmp_name):
                        os.unlink(tmp_name)
                except Exception:
                    pass

    def get(self, market_id: str) -> Optional[ContractSpec]:
        with self._lock:
            return self._specs.get(market_id)

    def set(self, spec: ContractSpec) -> None:
        with self._lock:
            self._specs[spec.market_id] = spec
        self._save()

    def update_status(
        self,
        market_id:           str,
        status:              MarketStatus,
        resolution_price:    Optional[float] = None,
        cancellation_reason: Optional[str]   = None,
        last_oracle_poll:    Optional[str]   = None,
    ) -> None:
        with self._lock:
            spec = self._specs.get(market_id)
            if spec is None:
                return
            spec.status = status
            if resolution_price    is not None: spec.resolution_price    = resolution_price
            if cancellation_reason is not None: spec.cancellation_reason = cancellation_reason
            if last_oracle_poll    is not None: spec.last_oracle_poll    = last_oracle_poll
        self._save()

    def known_ids(self) -> set:
        with self._lock:
            return set(self._specs.keys())

    def all(self) -> List[ContractSpec]:
        with self._lock:
            return list(self._specs.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._specs)