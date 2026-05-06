"""Alerters. Each module implements `Alerter` from socmon.interfaces."""

from __future__ import annotations

from typing import Type

from socmon.interfaces import Alerter

_REGISTRY: dict[str, Type[Alerter]] = {}


def register(type_name: str):
    def deco(cls: Type[Alerter]) -> Type[Alerter]:
        _REGISTRY[type_name] = cls
        return cls
    return deco


def build_alerter(type_name: str, **kwargs) -> Alerter:
    if type_name not in _REGISTRY:
        from socmon.alerters import slack, email, pagerduty, webhook  # noqa: F401
    if type_name not in _REGISTRY:
        raise KeyError(f"unknown alerter type: {type_name}")
    return _REGISTRY[type_name](**kwargs)
