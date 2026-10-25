"""
Strategy parameters: ``config/strategies.json`` with the historical environment
variables layered on top, so existing ``.env`` files keep working.

Environment overrides (legacy names):

- ``FINANCE_RT_MAX_BEST_ASK_USDC``   -> sniper.max_best_ask and forecast_edge.max_best_ask
- ``FINANCE_RT_MAX_BET_USDC``        -> sniper.max_bet_usdc (engine default as well)
- ``FINANCE_RT_PROB_TRADING``        -> forecast_edge.enabled
- ``FINANCE_RT_PROB_MIN_EDGE``       -> forecast_edge.min_edge
- ``FINANCE_RT_MAX_PROB_TARGETS``    -> forecast_edge.max_targets
- ``FINANCE_RT_MAX_TARGETS_PER_CYCLE`` -> sniper.max_targets
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger("strategies.config")
_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, dict[str, Any]] = {
    "sniper": {"enabled": True, "max_best_ask": 0.99, "max_bet_usdc": 50.0, "max_targets": 40},
    "forecast_edge": {
        "enabled": False,
        "min_edge": 0.08,
        "max_best_ask": 0.99,
        "max_bet_usdc": 25.0,
        "max_targets": 15,
        "require_full_day": True,
        "min_models": 1,
    },
}

_ENV_OVERRIDES: list[tuple[str, str, str, str]] = [
    # env name, strategy, parameter, type
    ("FINANCE_RT_MAX_BEST_ASK_USDC", "sniper", "max_best_ask", "float"),
    ("FINANCE_RT_MAX_BEST_ASK_USDC", "forecast_edge", "max_best_ask", "float"),
    ("FINANCE_RT_MAX_BET_USDC", "sniper", "max_bet_usdc", "float"),
    ("FINANCE_RT_MAX_TARGETS_PER_CYCLE", "sniper", "max_targets", "int"),
    ("FINANCE_RT_PROB_TRADING", "forecast_edge", "enabled", "bool"),
    ("FINANCE_RT_PROB_MIN_EDGE", "forecast_edge", "min_edge", "float"),
    ("FINANCE_RT_MAX_PROB_TARGETS", "forecast_edge", "max_targets", "int"),
]


def overrides_path() -> Path:
    """Operator toggles written by the dashboard; they win over the file and the environment."""
    raw = (os.getenv("STRATEGY_OVERRIDES") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else _ROOT / p
    return _ROOT / "data" / "strategy_overrides.json"


def load_overrides(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    p = path or overrides_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        LOG.warning("strategy overrides unreadable (%s): %s", p, e)
        return {}
    return {k: dict(v) for k, v in (data.get("strategies") or {}).items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def set_override(name: str, enabled: Optional[bool], *, path: Optional[Path] = None, who: str = "dashboard") -> dict[str, dict[str, Any]]:
    """Set (True/False) or clear (None) the operator's enabled flag for a strategy; atomic write."""
    p = path or overrides_path()
    current = load_overrides(p)
    if enabled is None:
        current.pop(name, None)
    else:
        current[name] = {"enabled": bool(enabled), "set_by": who, "set_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps({"_note": "Operator overrides from the dashboard; highest precedence.", "strategies": current}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return current


def config_path() -> Path:
    raw = (os.getenv("STRATEGIES_CONFIG") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else _ROOT / p
    return _ROOT / "config" / "strategies.json"


def _cast(value: str, typ: str) -> Any:
    v = value.strip()
    if typ == "bool":
        return v.lower() in ("1", "true", "yes", "on")
    if typ == "int":
        return int(float(v))
    return float(v)


@dataclass
class StrategyConfig:
    strategies: dict[str, dict[str, Any]] = field(default_factory=dict)
    path: Optional[Path] = None
    enabled_source: dict[str, str] = field(default_factory=dict)  # strategy -> default | file | env | override

    def params(self, name: str) -> dict[str, Any]:
        return dict(self.strategies.get(name, {}))

    def enabled_names(self) -> list[str]:
        return [n for n, p in self.strategies.items() if p.get("enabled", True)]

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path) if self.path else None, "strategies": self.strategies, "enabled_source": self.enabled_source}


def load_config(path: Optional[Path] = None, env: Optional[dict[str, str]] = None, overrides: Optional[Path] = None) -> StrategyConfig:
    """Defaults <- JSON file <- environment <- operator overrides (dashboard toggles)."""
    env = os.environ if env is None else env
    p = path or config_path()
    merged: dict[str, dict[str, Any]] = {k: dict(v) for k, v in DEFAULTS.items()}
    source: dict[str, str] = {k: "default" for k in DEFAULTS}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for name, params in (data.get("strategies") or data).items():
                if isinstance(params, dict):
                    merged.setdefault(name, {}).update(params)
                    if "enabled" in params:
                        source[name] = "file"
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        LOG.warning("strategies config unreadable (%s): %s", p, e)
    for env_name, strat, param, typ in _ENV_OVERRIDES:
        raw = env.get(env_name)
        if raw is None or not str(raw).strip():
            continue
        try:
            merged.setdefault(strat, {})[param] = _cast(str(raw), typ)
            if param == "enabled":
                source[strat] = "env"
        except ValueError:
            LOG.warning("ignoring %s=%r", env_name, raw)
    for name, ov in load_overrides(overrides).items():
        if "enabled" in ov:
            merged.setdefault(name, {})["enabled"] = bool(ov["enabled"])
            source[name] = "override"
    return StrategyConfig(strategies=merged, path=p if p.exists() else None, enabled_source=source)
