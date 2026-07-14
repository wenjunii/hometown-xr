"""Stable record identities and text fingerprints used across output formats."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

_WHITESPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"\w+", re.UNICODE)


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return _WHITESPACE.sub(" ", normalized).strip()


def normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().casefold()
    host = (parts.hostname or "").casefold()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme.casefold(), host, parts.path, parts.query, ""))


def _digest(parts: list[str]) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def stable_record_id(
    crawl_id: str,
    source_file: str,
    url: str,
    warc_date: str,
    paragraph: str,
) -> str:
    """Identify one captured paragraph without collapsing later recrawls."""
    return _digest(
        [
            crawl_id,
            source_file.replace("\\", "/"),
            normalize_url(url),
            warc_date,
            normalize_text(paragraph),
        ]
    )


def content_fingerprint(url: str, paragraph: str) -> str:
    """Identify exact normalized content independent of crawl provenance."""
    return _digest([normalize_url(url), normalize_text(paragraph)])


def text_fingerprint(text: str) -> str:
    """Identify normalized text independently of URL and crawl provenance."""
    return _digest([normalize_text(text)])


def story_fingerprint(text: str) -> str:
    """Return the stable canonical identifier used by story datasets."""
    return text_fingerprint(text)


def simhash64(text: str) -> int:
    """Return a locality-sensitive 64-bit fingerprint for near-duplicate text."""
    normalized = normalize_text(text)
    tokens = _TOKEN.findall(normalized)
    if len(tokens) < 5:
        tokens = [normalized[index : index + 3] for index in range(max(1, len(normalized) - 2))]
    weights = [0] * 64
    for token in tokens:
        value = int.from_bytes(
            hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(),
            "big",
        )
        for bit in range(64):
            weights[bit] += 1 if value & (1 << bit) else -1
    result = 0
    for bit, weight in enumerate(weights):
        if weight >= 0:
            result |= 1 << bit
    return result


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()
