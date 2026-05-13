"""Impersonation scoring.

Combines several signals into a single 0–100 score per candidate AccountObservation.

  Signal                              Weight   Notes
  ------                              ------   -----
  Username similarity                    25    rapidfuzz token-ratio vs legit handles
  Homoglyph / confusable detection       15    cyrillic/greek look-alikes
  Display-name similarity                15    fuzzy against brand + exec full names
  Bio similarity                         10    brand keyword presence in bio
  Avatar pHash distance                  20    imagehash hamming vs brand logos
  Account age (newer = riskier)          10    accounts <30d old get full weight
  Brand keyword in handle                 5    raw substring presence

Hard exclusions (score=0): handle (case-insensitive) is in `brand.legit_handles`
for the platform, OR matches any executive's `legit_handles` for the platform.

Severity bands:
  <40   discard (no finding emitted)
  40-69 medium (review queue)
  70-84 high
  85+   critical (likely exec impersonation; bumped if exec match)

The detector keeps a "scored accounts" hash in kv_state so unchanged accounts
don't regenerate identical findings each cycle. Findings themselves are dedup'd
by (detector, account_id) via the deterministic id helper.
"""

from __future__ import annotations

import hashlib
import logging
import unicodedata
from datetime import datetime, timezone
from typing import Iterator

import imagehash
from PIL import Image
from rapidfuzz import fuzz

from socmon.config import BrandEntity, ExecutiveEntity
from socmon.dedup import finding_id
from socmon.detectors import register
from socmon.interfaces import Detector
from socmon.models import (
    AccountObservation,
    EvidenceRef,
    Finding,
    FindingKind,
    Observation,
    ObservationKind,
    Severity,
    TimeWindow,
)
from socmon.storage.base import Storage

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Confusables — homoglyph table (small, hand-curated)
# ---------------------------------------------------------------------------
# We deliberately keep this short. Unicode's full confusables table is huge but
# 95% of real-world impersonations use the dozen-or-so swaps below.

_CONFUSABLES: dict[str, str] = {
    # Cyrillic look-alikes
    "а": "a",  # а
    "е": "e",  # е
    "о": "o",  # о
    "с": "c",  # с
    "р": "p",  # р
    "х": "x",  # х
    "у": "y",  # у
    "і": "i",  # і
    "ј": "j",  # ј
    "һ": "h",  # һ
    # Greek
    "ο": "o",  # ο
    "α": "a",  # α
    "ε": "e",  # ε
    "ρ": "p",  # ρ
    # Common ASCII swaps
    "0": "o",
    "1": "l",
    "3": "e",
    "4": "a",
    "5": "s",
    "$": "s",
    "@": "a",
}


def _normalize_confusables(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    return "".join(_CONFUSABLES.get(ch, ch) for ch in s)


def _has_confusables(s: str) -> bool:
    """True iff normalizing changes the string (i.e. it contained any confusable)."""
    return s.lower() != _normalize_confusables(s)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


# Severity thresholds.
_BAND_MEDIUM = 40
_BAND_HIGH = 70
_BAND_CRITICAL = 85
_AVATAR_PHASH_MAX_DIFF = 64  # phash is 64-bit


def _username_similarity(handle: str, legit: list[str]) -> tuple[float, str | None]:
    """Best ratio (0–1) of `handle` against any legit handle. Returns the matched
    legit handle so evidence can name it.
    """
    if not legit:
        return 0.0, None
    handle_n = _normalize_confusables(handle)
    best = 0.0
    best_h = None
    for h in legit:
        h_n = _normalize_confusables(h)
        # token_set_ratio handles minor reorders ("acme_official" vs "official_acme")
        r = max(
            fuzz.ratio(handle_n, h_n),
            fuzz.partial_ratio(handle_n, h_n),
        ) / 100.0
        if r > best:
            best = r
            best_h = h
    return best, best_h


def _display_name_similarity(name: str | None, targets: list[str]) -> float:
    if not name or not targets:
        return 0.0
    n = _normalize_confusables(name)
    return max(fuzz.token_set_ratio(n, _normalize_confusables(t)) for t in targets) / 100.0


def _phash_distance(account_phash: str | None, brand_phashes: list[str]) -> int | None:
    """Min hamming distance vs any brand logo, or None if we can't compare."""
    if not account_phash or not brand_phashes:
        return None
    try:
        ah = imagehash.hex_to_hash(account_phash)
    except (ValueError, TypeError):
        return None
    best: int | None = None
    for bp in brand_phashes:
        try:
            bh = imagehash.hex_to_hash(bp)
        except (ValueError, TypeError):
            continue
        d = ah - bh
        if best is None or d < best:
            best = d
    return best


def _account_age_days(account: AccountObservation, now: datetime) -> float | None:
    if not account.account_created_at:
        return None
    delta = now - account.account_created_at
    return max(delta.total_seconds() / 86400.0, 0.0)


def score_account(
    account: AccountObservation,
    brand: BrandEntity,
    executives: list[ExecutiveEntity],
    brand_phashes: list[str],
    now: datetime | None = None,
) -> tuple[float, dict]:
    """Pure function: returns (score 0–100, breakdown dict).

    The breakdown is preserved on the Finding so reviewers can see *why* something
    scored where it did.
    """
    now = now or datetime.now(timezone.utc)
    handle = (account.author_handle or "").lower()
    breakdown: dict = {"signals": {}, "matched_legit": None, "matched_exec": None}

    # ----- Hard exclusions -----
    legit_handles = {h.lower() for h in brand.legit_handles.get(account.platform, [])}
    for exec_ in executives:
        legit_handles |= {h.lower() for h in exec_.legit_handles.get(account.platform, [])}
    if handle and handle in legit_handles:
        breakdown["excluded"] = "legitimate_handle"
        return 0.0, breakdown

    # ----- Username similarity (25) -----
    brand_handles = brand.legit_handles.get(account.platform, [])
    u_sim, u_match = _username_similarity(handle, brand_handles)
    breakdown["signals"]["username_similarity"] = round(u_sim, 3)
    breakdown["matched_legit"] = u_match
    # Also test against each exec's legit handles separately — exec hits are
    # routed to the critical band later.
    exec_username_hit: tuple[ExecutiveEntity, float] | None = None
    for exec_ in executives:
        e_legit = exec_.legit_handles.get(account.platform, [])
        e_sim, _ = _username_similarity(handle, e_legit)
        if e_sim > 0.7 and (exec_username_hit is None or e_sim > exec_username_hit[1]):
            exec_username_hit = (exec_, e_sim)
    if exec_username_hit:
        breakdown["matched_exec"] = exec_username_hit[0].name
        u_sim = max(u_sim, exec_username_hit[1])
    score_username = u_sim * 25

    # ----- Homoglyph (15) — only counts if there's some username similarity -----
    if (handle and _has_confusables(handle)) and u_sim > 0.6:
        score_homoglyph = 15.0
        breakdown["signals"]["homoglyph"] = True
    else:
        score_homoglyph = 0.0
        breakdown["signals"]["homoglyph"] = False

    # ----- Display-name similarity (15) -----
    name_targets = [brand.name, *brand.aliases, *(e.name for e in executives)]
    d_sim = _display_name_similarity(account.display_name, name_targets)
    breakdown["signals"]["display_name_similarity"] = round(d_sim, 3)
    score_display = d_sim * 15

    # ----- Bio similarity (10) — brand keyword presence -----
    bio = (account.bio or "").lower()
    bio_terms = [brand.name.lower(), *(a.lower() for a in brand.aliases),
                 *(d.lower() for d in brand.domains)]
    bio_hits = sum(1 for t in bio_terms if t and t in bio)
    bio_ratio = min(bio_hits / max(len(bio_terms), 1) * 3, 1.0)  # saturate quickly
    breakdown["signals"]["bio_brand_terms"] = bio_hits
    score_bio = bio_ratio * 10

    # ----- Avatar pHash (20) -----
    phash_dist = _phash_distance(account.avatar_phash, brand_phashes)
    if phash_dist is None:
        score_avatar = 0.0
        breakdown["signals"]["avatar_phash_distance"] = None
    else:
        # Closer = higher score. Fully identical = full weight; >25 hamming = ~zero.
        breakdown["signals"]["avatar_phash_distance"] = phash_dist
        score_avatar = max(0.0, 1.0 - (phash_dist / 25.0)) * 20

    # ----- Account age (10) -----
    age_days = _account_age_days(account, now)
    if age_days is None:
        score_age = 0.0
        breakdown["signals"]["account_age_days"] = None
    elif age_days <= 30:
        score_age = 10.0
        breakdown["signals"]["account_age_days"] = age_days
    elif age_days <= 180:
        # Linearly decay 10→0 over the next 5 months.
        score_age = 10.0 * (1.0 - (age_days - 30) / 150.0)
        breakdown["signals"]["account_age_days"] = age_days
    else:
        score_age = 0.0
        breakdown["signals"]["account_age_days"] = age_days

    # ----- Brand keyword in handle (5) -----
    handle_norm = _normalize_confusables(handle)
    keyword_terms = [brand.name.lower(), *(a.lower() for a in brand.aliases)]
    handle_kw_hit = any(t and t in handle_norm for t in keyword_terms)
    score_handle_kw = 5.0 if handle_kw_hit else 0.0
    breakdown["signals"]["brand_keyword_in_handle"] = handle_kw_hit

    total = (
        score_username
        + score_homoglyph
        + score_display
        + score_bio
        + score_avatar
        + score_age
        + score_handle_kw
    )

    breakdown["weighted"] = {
        "username": round(score_username, 2),
        "homoglyph": round(score_homoglyph, 2),
        "display": round(score_display, 2),
        "bio": round(score_bio, 2),
        "avatar": round(score_avatar, 2),
        "age": round(score_age, 2),
        "handle_kw": round(score_handle_kw, 2),
    }
    return min(round(total, 2), 100.0), breakdown


def severity_for(score: float, hit_exec: bool) -> Severity | None:
    """None means the finding is below the medium band and should be discarded."""
    if score < _BAND_MEDIUM:
        return None
    if score >= _BAND_CRITICAL or (hit_exec and score >= _BAND_HIGH):
        return Severity.CRITICAL
    if score >= _BAND_HIGH:
        return Severity.HIGH
    return Severity.MEDIUM


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


@register("impersonation")
class ImpersonationDetector(Detector):
    name = "impersonation"

    def __init__(
        self,
        brand: BrandEntity | None = None,
        executives: list[ExecutiveEntity] | None = None,
        name: str | None = None,
        **options,
    ) -> None:
        if brand is None:
            raise ValueError("ImpersonationDetector requires a BrandEntity")
        self.brand = brand
        self.executives = executives or []
        if name:
            self.name = name
        self.options = options
        self.min_band: Severity = Severity(options.get("min_band", "medium"))
        self.brand_phashes: list[str] = self._compute_brand_phashes(brand)

    def run(self, storage: Storage, window: TimeWindow) -> Iterator[Finding]:
        for obs in storage.query_observations(
            kind=ObservationKind.ACCOUNT.value,
            since=window.start,
            until=window.end,
        ):
            if not isinstance(obs, AccountObservation):
                continue
            finding = self._evaluate(obs, storage)
            if finding is not None:
                yield finding

    # ----- internals -----

    def _evaluate(self, account: AccountObservation, storage: Storage) -> Finding | None:
        # Fast path: skip accounts whose content hasn't changed since we last scored them.
        sig = self._account_signature(account)
        seen_key = f"{account.platform}:{account.author_id}"
        last_sig = storage.get_state(self.name, seen_key)
        if last_sig == sig:
            return None

        now = datetime.now(timezone.utc)
        score, breakdown = score_account(
            account=account,
            brand=self.brand,
            executives=self.executives,
            brand_phashes=self.brand_phashes,
            now=now,
        )

        # Mark the signature as seen even on misses, so we don't re-score every poll.
        storage.set_state(self.name, seen_key, sig)

        hit_exec = breakdown.get("matched_exec") is not None
        sev = severity_for(score, hit_exec=hit_exec)
        if sev is None:
            return None

        fid = finding_id(
            detector=self.name,
            entity_key=f"{account.platform}:{account.author_id}",
            bucket_start=now.replace(hour=0, minute=0, second=0, microsecond=0),
            bucket_seconds=86400,
        )
        # Severity and score are rendered by the CLI / alerter formatters; the
        # title carries the *what* and *who*, not the *how bad*.
        if breakdown.get("matched_exec"):
            title = (
                f"Exec impersonation of {breakdown['matched_exec']}: "
                f"{account.platform}/{account.author_handle}"
            )
        else:
            title = f"Possible impersonation: {account.platform}/{account.author_handle}"
        summary = (
            f"Account {account.author_handle!r} on {account.platform} resembles "
            f"{breakdown.get('matched_legit') or self.brand.name!r}. "
            f"Signals: {breakdown.get('signals')}"
        )

        return Finding(
            id=fid,
            kind=FindingKind.IMPERSONATION,
            detector=self.name,
            severity=sev,
            score=score,
            title=title,
            summary=summary,
            detected_at=now,
            evidence=[
                EvidenceRef(
                    observation_id=account.id,
                    platform=account.platform,
                    url=str(account.url) if account.url else None,
                    snippet=account.bio,
                )
            ],
            metadata=breakdown,
        )

    def _account_signature(self, account: AccountObservation) -> str:
        h = hashlib.sha256()
        for part in (
            account.author_handle or "",
            account.display_name or "",
            account.bio or "",
            account.avatar_phash or "",
        ):
            h.update(part.encode("utf-8"))
            h.update(b"\x1f")
        return h.hexdigest()

    @staticmethod
    def _compute_brand_phashes(brand: BrandEntity) -> list[str]:
        """Hash each local logo path at startup. Failures are logged and skipped — the
        rest of the detector still works, just without the avatar signal."""
        out: list[str] = []
        for path in brand.logo_paths:
            try:
                with Image.open(path) as img:
                    out.append(str(imagehash.phash(img)))
            except (FileNotFoundError, OSError, ValueError) as e:
                log.warning("could not phash brand logo %r: %s", path, e)
        return out
