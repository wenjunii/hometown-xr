"""Conservative corpus quality and diversity diagnostics."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit

from config import DOMAIN_SHARE_WARNING
from record_identity import normalize_text

_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_NUMBER = re.compile(r"\b\d+(?:[.,:/-]\d+)*\b")
_TOKEN = re.compile(r"\w+", re.UNICODE)

_NAVIGATION_PHRASES = (
    "skip to content",
    "main menu",
    "navigation",
    "home about contact",
    "previous post",
    "next post",
    "back to top",
)
_POLICY_PHRASES = (
    "cookie policy",
    "privacy policy",
    "terms and conditions",
    "all rights reserved",
    "accept cookies",
)
_PROMOTION_PHRASES = (
    "subscribe to our newsletter",
    "sign up for our newsletter",
    "add to cart",
    "buy now",
    "limited time offer",
)
_METADATA_PHRASES = (
    "posted in",
    "filed under",
    "leave a comment",
    "share this post",
    "related posts",
)

_LYRICS_DOMAINS = (
    "lyricsmode.com",
    "ohhla.com",
    "sing365.com",
    "azlyrics.com",
    "genius.com",
    "metrolyrics.com",
)
_POETRY_DOMAINS = ("poetrysoup.com", "allpoetry.com", "poemhunter.com")
_ADULT_DOMAINS = ("asstr.org", "literotica.com", "storiesonline.net")
_GENEALOGY_DOMAINS = ("ancestry.com", "familysearch.org", "genealogy.com")
_COMMERCIAL_DOMAINS = ("amazon.", "ebay.", "etsy.com", "tripadvisor.")
_LYRIC_MARKER = re.compile(
    r"\[(?:verse|chorus|bridge|hook|intro|outro)(?:\s+\d+)?\]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ContentClassification:
    category: str
    confidence: float
    flags: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_record_fields(self) -> dict:
        return {
            "content_category": self.category,
            "content_confidence": round(self.confidence, 4),
            "content_flags": list(self.flags),
            "content_reasons": list(self.reasons),
        }


def domain_from_url(url: str) -> str:
    """Return a normalized host suitable for diversity accounting."""
    try:
        hostname = (urlsplit(url).hostname or "").casefold().strip(".")
    except ValueError:
        return "unknown"
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname or "unknown"


def _domain_matches(domain: str, candidates: tuple[str, ...]) -> bool:
    return any(
        candidate in domain if candidate.endswith(".") else domain.endswith(candidate)
        for candidate in candidates
    )


def classify_content(text: str, url: str = "") -> ContentClassification:
    """Classify accepted text for curation while preserving the original record."""
    normalized = normalize_text(text)
    domain = domain_from_url(url)

    if _domain_matches(domain, _ADULT_DOMAINS) or any(
        phrase in normalized for phrase in ("erotic story", "adult sex story")
    ):
        return ContentClassification(
            "adult_content",
            0.99 if _domain_matches(domain, _ADULT_DOMAINS) else 0.82,
            ("sensitive", "exclude_from_default_curated"),
            ("adult_source_or_marker",),
        )

    lyric_signals = [
        _domain_matches(domain, _LYRICS_DOMAINS),
        bool(_LYRIC_MARKER.search(text)),
        "song lyrics" in normalized,
        "lyrics by" in normalized,
        "source: http://www.sing365.com" in normalized,
        "album:" in normalized and "artist:" in normalized,
    ]
    if lyric_signals[0] or sum(lyric_signals[1:]) >= 2:
        return ContentClassification(
            "lyrics",
            0.99 if lyric_signals[0] else 0.86,
            ("creative_work", "exclude_from_default_curated"),
            ("lyrics_source" if lyric_signals[0] else "lyrics_markers",),
        )

    poetry_domain = _domain_matches(domain, _POETRY_DOMAINS)
    poetry_markers = sum(
        phrase in normalized
        for phrase in ("poem by", "poetry contest", "read my poem", "published poem")
    )
    if poetry_domain or poetry_markers >= 2:
        return ContentClassification(
            "poetry",
            0.98 if poetry_domain else 0.78,
            ("creative_work", "exclude_from_default_curated"),
            ("poetry_source" if poetry_domain else "poetry_markers",),
        )

    commercial_domain = _domain_matches(domain, _COMMERCIAL_DOMAINS)
    commercial_signals = sum(
        phrase in normalized
        for phrase in (
            "add to cart",
            "buy now",
            "sale price",
            "book your stay",
            "customer reviews",
            "free shipping",
        )
    )
    if commercial_domain or commercial_signals >= 2:
        return ContentClassification(
            "commercial",
            0.94 if commercial_domain else 0.82,
            ("commercial", "exclude_from_default_curated"),
            ("commercial_source" if commercial_domain else "commercial_markers",),
        )

    genealogy_domain = _domain_matches(domain, _GENEALOGY_DOMAINS)
    genealogy_signals = sum(
        phrase in normalized
        for phrase in (
            "family tree",
            "genealogy record",
            "ancestry record",
            "was born on",
            "died on",
            "married on",
        )
    )
    if genealogy_domain or genealogy_signals >= 2:
        return ContentClassification(
            "genealogy",
            0.96 if genealogy_domain else 0.78,
            ("reference_material", "exclude_from_default_curated"),
            ("genealogy_source" if genealogy_domain else "genealogy_markers",),
        )

    return ContentClassification("personal_prose", 0.72, (), ("no_exclusion_signals",))


def template_fingerprint(text: str) -> str:
    """Hash structural text after replacing volatile URL, email, and number fields."""
    normalized = normalize_text(text)
    normalized = _URL.sub("<url>", normalized)
    normalized = _EMAIL.sub("<email>", normalized)
    normalized = _NUMBER.sub("<number>", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def concept_cluster_id(concept: str) -> str:
    normalized = normalize_text(concept)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def boilerplate_features(text: str) -> list[str]:
    """Return explainable warning signals without discarding the record."""
    normalized = normalize_text(text)
    features = []
    if sum(phrase in normalized for phrase in _NAVIGATION_PHRASES) >= 2:
        features.append("navigation")
    if any(phrase in normalized for phrase in _POLICY_PHRASES):
        features.append("policy")
    if any(phrase in normalized for phrase in _PROMOTION_PHRASES):
        features.append("promotion")
    if sum(phrase in normalized for phrase in _METADATA_PHRASES) >= 2:
        features.append("post_metadata")
    if len(_URL.findall(text)) >= 4:
        features.append("link_heavy")

    tokens = _TOKEN.findall(normalized)
    if len(tokens) >= 60 and len(set(tokens)) / len(tokens) < 0.28:
        features.append("low_lexical_diversity")

    lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
    if len(lines) >= 6 and len(set(lines)) / len(lines) < 0.6:
        features.append("repeated_lines")
    return features


def boilerplate_score(features: list[str]) -> int:
    weights = {
        "navigation": 3,
        "policy": 2,
        "promotion": 2,
        "post_metadata": 2,
        "link_heavy": 2,
        "low_lexical_diversity": 2,
        "repeated_lines": 3,
    }
    return sum(weights.get(feature, 1) for feature in features)


class DiversityTracker:
    """Build bounded summary diagnostics while canonical stories stream past."""

    def __init__(self, domain_share_warning: float = DOMAIN_SHARE_WARNING):
        if not 0 < domain_share_warning <= 1:
            raise ValueError("domain_share_warning must be between 0 and 1")
        self.domain_share_warning = domain_share_warning
        self.domains = Counter()
        self.templates = Counter()
        self.concepts = Counter()
        self.languages = Counter()
        self.categories = Counter()
        self.boilerplate_candidates = 0
        self.curated_default = 0
        self.total = 0

    def observe(self, record: dict) -> None:
        self.total += 1
        self.domains[str(record.get("domain", "unknown"))] += 1
        self.templates[str(record.get("template_fingerprint", ""))] += 1
        self.concepts[str(record.get("concept_match", "unknown"))] += 1
        self.languages[str(record.get("language", "unknown"))] += 1
        self.categories[str(record.get("content_category", "unknown"))] += 1
        if int(record.get("boilerplate_score", 0)) >= 4:
            self.boilerplate_candidates += 1
        if bool(record.get("curated_default")):
            self.curated_default += 1

    @staticmethod
    def _top(counter: Counter, limit: int = 20) -> list[dict]:
        return [
            {"value": value, "stories": count}
            for value, count in counter.most_common(limit)
        ]

    def report(self) -> dict:
        top_domain_count = self.domains.most_common(1)[0][1] if self.domains else 0
        top_domain_share = top_domain_count / self.total if self.total else 0.0
        repeated_templates = [
            {"template_fingerprint": value, "stories": count}
            for value, count in self.templates.most_common(20)
            if count > 1
        ]
        return {
            "canonical_stories": self.total,
            "unique_domains": len(self.domains),
            "unique_languages": len(self.languages),
            "concept_clusters": len(self.concepts),
            "boilerplate_candidates": self.boilerplate_candidates,
            "curated_default_stories": self.curated_default,
            "top_domain_share": round(top_domain_share, 4),
            "domain_share_warning_threshold": self.domain_share_warning,
            "domain_concentration_warning": top_domain_share > self.domain_share_warning,
            "top_domains": self._top(self.domains),
            "top_languages": self._top(self.languages),
            "content_categories": self._top(self.categories),
            "top_concepts": self._top(self.concepts),
            "repeated_templates": repeated_templates,
        }
