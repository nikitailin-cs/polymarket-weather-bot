"""Source registry: name -> factory, and the ``SOURCES`` environment variable."""
from __future__ import annotations

import os
from typing import Any, Callable

from core.sources.base import Source


def _metar_ws(**kw: Any) -> Source:
    from core.sources.metar_ws import MetarWsSource

    return MetarWsSource(**kw)


def _awc_poll(**kw: Any) -> Source:
    from core.sources.awc_poll import AwcPollSource

    return AwcPollSource(**kw)


REGISTRY: dict[str, Callable[..., Source]] = {
    "metar_ws": _metar_ws,
    "awc_poll": _awc_poll,
}

_ALIASES = {"metar.ws": "metar_ws", "metarws": "metar_ws", "ws": "metar_ws", "awc": "awc_poll", "noaa": "awc_poll", "poll": "awc_poll"}


def source_names_from_env(default: str = "metar_ws") -> list[str]:
    raw = (os.getenv("SOURCES") or default).strip()
    names: list[str] = []
    for part in raw.split(","):
        n = part.strip().lower()
        if not n:
            continue
        n = _ALIASES.get(n, n)
        if n not in names:
            names.append(n)
    return names


def build_sources(names: list[str], **per_source_kwargs: dict[str, Any]) -> list[Source]:
    """Instantiate the named sources; unknown names raise ``KeyError`` with the known list."""
    out: list[Source] = []
    for n in names:
        if n not in REGISTRY:
            raise KeyError(f"unknown source {n!r}; known: {', '.join(sorted(REGISTRY))}")
        out.append(REGISTRY[n](**per_source_kwargs.get(n, {})))
    return out
