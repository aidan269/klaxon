"""Configuration schema (pydantic) + YAML loader.

One file describes everything: what we're monitoring, which platforms to poll, what
detectors to run, where alerts go. Credentials are referenced by env-var name — the
config file itself never contains secrets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from socmon.models import FindingKind, Severity


# ---------------------------------------------------------------------------
# Entities — what we're protecting
# ---------------------------------------------------------------------------


class BrandEntity(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)  # used for typosquat detection
    # platform -> list of legitimate handles. Anything resembling these but NOT in this list
    # is a candidate for impersonation.
    legit_handles: dict[str, list[str]] = Field(default_factory=dict)
    # Local paths (or URLs) of canonical brand assets. pHashes computed at startup
    # and used by the impersonation detector.
    logo_paths: list[str] = Field(default_factory=list)


class ExecutiveEntity(BaseModel):
    name: str
    title: str
    legit_handles: dict[str, list[str]] = Field(default_factory=dict)
    # If True, impersonation findings against this exec route to higher severity.
    high_value_target: bool = False


class Keyword(BaseModel):
    """Tracked term for the keyword spike detector.

    `expr` supports a small DSL: bare terms, AND/OR/NOT, quoted phrases, and NEAR/N
    proximity. e.g.  '"acme" AND ("breach" OR "leak" OR "0day") NEAR/10 customer'
    """
    expr: str
    severity: Severity = Severity.MEDIUM
    label: str | None = None  # human-friendly name; falls back to expr


# ---------------------------------------------------------------------------
# Source-of-truth feeds
# ---------------------------------------------------------------------------


class LegitJobsSource(BaseModel):
    """Where to pull the canonical list of real openings to compare against."""
    kind: Literal["careers_page", "greenhouse", "lever", "ashby", "static_yaml"]
    url: str | None = None
    path: str | None = None  # for static_yaml
    options: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Plugin configs
# ---------------------------------------------------------------------------


class CollectorConfig(BaseModel):
    name: str  # instance name; type is determined by `type` so multiple of the same kind work
    type: str  # registry key, e.g. "reddit", "rss", "twitter", "bluesky"
    enabled: bool = True
    poll_interval_seconds: int = 300
    credentials_env: dict[str, str] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class DetectorConfig(BaseModel):
    name: str
    type: Literal["mention_spike", "keyword_spike", "impersonation", "fake_job"]
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


class AlerterConfig(BaseModel):
    name: str
    type: Literal["slack", "email", "pagerduty", "webhook"]
    options: dict[str, Any] = Field(default_factory=dict)


class AlertRoute(BaseModel):
    """Routes findings to alerters. First matching route wins (order matters)."""
    match_kind: FindingKind | Literal["*"] = "*"
    match_detector: str | None = None  # glob-ish; None = any
    severity_min: Severity = Severity.MEDIUM
    channels: list[str]  # alerter names
    digest: bool = False  # if True, batched into daily/weekly digest instead of immediate


class StorageConfig(BaseModel):
    backend: Literal["sqlite", "postgres"] = "sqlite"
    dsn: str = "sqlite:///socmon.db"


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


class SocmonConfig(BaseModel):
    organization: str
    brand: BrandEntity
    executives: list[ExecutiveEntity] = Field(default_factory=list)
    keywords: list[Keyword] = Field(default_factory=list)

    legit_jobs: LegitJobsSource | None = None

    # Detector defaults; individual detectors can override via their own `options`.
    baseline_window_days: int = 7
    spike_z_threshold: float = 3.0
    spike_min_volume: int = 5  # ignore "spikes" from a baseline of ~zero

    # Continuous-mode cadence. `socmon run` ticks every enabled collector on its
    # own `poll_interval_seconds` and ticks all detectors together on this
    # interval. 5 minutes is the production sweet spot; tighter is fine for
    # demos but Reddit's anonymous rate limit (~60 req/min) puts a real floor
    # around ~1 min. See the Scheduling section of the README.
    detector_interval_seconds: int = 300

    storage: StorageConfig = Field(default_factory=StorageConfig)
    collectors: list[CollectorConfig] = Field(default_factory=list)
    detectors: list[DetectorConfig] = Field(default_factory=list)
    alerters: list[AlerterConfig] = Field(default_factory=list)
    routes: list[AlertRoute] = Field(default_factory=list)

    @field_validator("collectors", "detectors", "alerters")
    @classmethod
    def _names_unique(cls, v: list[Any]) -> list[Any]:
        names = [item.name for item in v]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate names: {names}")
        return v


def load_config(path: str | Path) -> SocmonConfig:
    """Load YAML, validate, return a SocmonConfig."""
    text = Path(path).read_text()
    data = yaml.safe_load(text)
    return SocmonConfig.model_validate(data)
