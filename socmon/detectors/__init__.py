"""Detectors. Each module implements `Detector` from socmon.interfaces.

The four built-ins map 1:1 to the spec:
  - mention_spike  : volume of brand mentions across all platforms vs rolling baseline
  - keyword_spike  : same math, but per configured keyword/expression
  - impersonation  : score candidate accounts against legitimate handles + brand assets
  - fake_job       : compare observed job listings against the source-of-truth feed
"""

from __future__ import annotations

from typing import Type

from socmon.interfaces import Detector

_REGISTRY: dict[str, Type[Detector]] = {}


def register(type_name: str):
    def deco(cls: Type[Detector]) -> Type[Detector]:
        _REGISTRY[type_name] = cls
        return cls
    return deco


def build_detector(type_name: str, **kwargs) -> Detector:
    if type_name not in _REGISTRY:
        from socmon.detectors import mention_spike, keyword_spike, impersonation, fake_job  # noqa: F401
    if type_name not in _REGISTRY:
        raise KeyError(f"unknown detector type: {type_name}")
    return _REGISTRY[type_name](**kwargs)
