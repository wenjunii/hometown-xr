"""Conservative corpus quality and diversity diagnostics."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
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


def domain_from_url(url: str) -> str:
    """Return a normalized host suitable for diversity accounting."""
    try:
        hostname = (urlsplit(url).hostname or "").casefold().strip(".")
    except ValueError:
        return "unknown"
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname or "unknown"


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
        self.boilerplate_candidates = 0
        self.total = 0

    def observe(self, record: dict) -> None:
        self.total += 1
        self.domains[str(record.get("domain", "unknown"))] += 1
        self.templates[str(record.get("template_fingerprint", ""))] += 1
        self.concepts[str(record.get("concept_match", "unknown"))] += 1
        self.languages[str(record.get("language", "unknown"))] += 1
        if int(record.get("boilerplate_score", 0)) >= 4:
            self.boilerplate_candidates += 1

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
            "top_domain_share": round(top_domain_share, 4),
            "domain_share_warning_threshold": self.domain_share_warning,
            "domain_concentration_warning": top_domain_share > self.domain_share_warning,
            "top_domains": self._top(self.domains),
            "top_languages": self._top(self.languages),
            "top_concepts": self._top(self.concepts),
            "repeated_templates": repeated_templates,
        }
