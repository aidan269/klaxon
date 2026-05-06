"""Platform adapters. Each module implements `Collector` from socmon.interfaces.

Registry pattern: collectors register themselves so `CollectorConfig.type` strings
in YAML resolve to the right class without a hard import list.
"""

from __future__ import annotations

from typing import Type

from socmon.interfaces import Collector

_REGISTRY: dict[str, Type[Collector]] = {}


def register(type_name: str):
    def deco(cls: Type[Collector]) -> Type[Collector]:
        _REGISTRY[type_name] = cls
        return cls
    return deco


def build_collector(type_name: str, **kwargs) -> Collector:
    if type_name not in _REGISTRY:
        # Lazy-import builtins so optional deps don't break import-time.
        from socmon.collectors import reddit, rss, twitter, linkedin, bluesky  # noqa: F401
    if type_name not in _REGISTRY:
        raise KeyError(f"unknown collector type: {type_name}")
    return _REGISTRY[type_name](**kwargs)
