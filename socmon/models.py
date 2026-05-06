"""Core data models — what gets collected, what detectors emit, what alerters consume.

Two-tier design:
  - Observations: normalized records of things seen in the wild (posts, accounts, jobs).
    Stored raw so detectors can be re-run on history.
  - Findings: derived signals produced by detectors (a spike, an impersonation candidate,
    a suspicious job). Findings reference the observations that produced them.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ObservationKind(str, Enum):
    POST = "post"          # a piece of content (tweet, reddit post, news article, etc.)
    COMMENT = "comment"    # reply / sub-content tied to a post
    ACCOUNT = "account"    # a profile snapshot, used by impersonation detector
    JOB = "job"            # a job listing, used by fake-job detector


class FindingKind(str, Enum):
    MENTION_SPIKE = "mention_spike"
    KEYWORD_SPIKE = "keyword_spike"
    IMPERSONATION = "impersonation"
    FAKE_JOB = "fake_job"


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


class Observation(BaseModel):
    """One thing seen on one platform at one time. Storage-canonical."""

    # Deterministic id: f"{platform}:{kind}:{platform_native_id}".
    # Used as primary key so re-collecting the same item is idempotent.
    id: str
    platform: str
    kind: ObservationKind

    # Who/what produced it. author_id is the platform's stable id (not display name)
    # because handles can be renamed.
    author_handle: str | None = None
    author_id: str | None = None

    text: str | None = None
    url: HttpUrl | str | None = None

    created_at: datetime  # when the content was created on-platform
    collected_at: datetime  # when we observed it

    # Free-form platform-native payload, kept verbatim so we can re-derive fields later
    # if normalization changes. Detectors should NOT depend on this directly.
    raw: dict[str, Any] = Field(default_factory=dict)

    # Subclasses fill these where relevant.
    extra: dict[str, Any] = Field(default_factory=dict)


class AccountObservation(Observation):
    kind: Literal[ObservationKind.ACCOUNT] = ObservationKind.ACCOUNT
    display_name: str | None = None
    bio: str | None = None
    avatar_url: str | None = None
    avatar_phash: str | None = None  # perceptual hash, hex-encoded
    follower_count: int | None = None
    account_created_at: datetime | None = None
    verified: bool | None = None


class JobObservation(Observation):
    kind: Literal[ObservationKind.JOB] = ObservationKind.JOB
    title: str
    company_claimed: str  # the company name as shown in the listing
    location: str | None = None
    salary_text: str | None = None
    apply_url: str | None = None
    posted_by_handle: str | None = None
    contact_methods: list[str] = Field(default_factory=list)  # whatsapp/telegram/email handles found in body


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


class EvidenceRef(BaseModel):
    """Pointer to an Observation, kept lightweight so findings can serialize cheaply."""
    observation_id: str
    platform: str
    url: str | None = None
    snippet: str | None = None


class Finding(BaseModel):
    """A detector's output. Deduplicated by `id` (a deterministic content hash)."""

    id: str  # e.g. sha256(detector + entity + window) — see dedup.py
    kind: FindingKind
    detector: str  # detector instance name (so two detectors of same kind can be told apart)
    severity: Severity
    score: float = Field(ge=0, le=100)

    title: str
    summary: str

    detected_at: datetime
    window_start: datetime | None = None
    window_end: datetime | None = None

    evidence: list[EvidenceRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Query / window types passed to collectors and detectors
# ---------------------------------------------------------------------------


class CollectorQuery(BaseModel):
    """Tells a collector what to fetch. Collectors may ignore fields they can't honor."""
    keywords: list[str] = Field(default_factory=list)
    handles: list[str] = Field(default_factory=list)  # accounts to monitor specifically
    since: datetime | None = None
    limit: int | None = None


class TimeWindow(BaseModel):
    start: datetime
    end: datetime
