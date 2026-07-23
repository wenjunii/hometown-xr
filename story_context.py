"""Expand one precisely matched paragraph into bounded document context."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from config import (
    PASSAGE_MAX_CHARS,
    PASSAGE_MAX_PARAGRAPHS,
    STORY_CONTEXT_AFTER_PARAGRAPHS,
    STORY_CONTEXT_BEFORE_PARAGRAPHS,
    STORY_EXPANSION_VERSION,
    STORY_MIN_CHARS,
    STORY_MIN_SENTENCES,
)

_SENTENCE_END = re.compile(r"[.!?\u3002\uff01\uff1f]+(?:[\"'\u201d\u2019)\]]+)?")
_SEPARATOR = re.compile(r"^(?:[-=_*#~]\s*){3,}$")
_HEADING_PREFIX = re.compile(
    r"^(?:chapter|part|section|book|appendix|contents?|references?)\b",
    re.IGNORECASE,
)
_SALUTATION = re.compile(
    r"^(?:dear|hello|hi|to whom it may concern)\b.{0,100}[:,]?$",
    re.IGNORECASE,
)
_WORD = re.compile(r"\b[\w'\u2019-]+\b", re.UNICODE)


@dataclass(frozen=True)
class StoryWindow:
    """A seed paragraph plus explicitly role-labeled surrounding context."""

    payload: dict


def sentence_count(text: str) -> int:
    """Count conservative sentence-ending punctuation across common scripts."""
    return len(_SENTENCE_END.findall(text))


def _looks_like_heading(text: str) -> bool:
    value = " ".join(text.split()).strip()
    if not value:
        return True
    if _SEPARATOR.fullmatch(value):
        return True
    if len(value) > 120 or sentence_count(value):
        return False
    words = _WORD.findall(value)
    if not words or len(words) > 14:
        return False
    letters = [character for character in value if character.isalpha()]
    uppercase_share = (
        sum(character.isupper() for character in letters) / len(letters)
        if letters
        else 0.0
    )
    title_share = sum(word[:1].isupper() for word in words) / len(words)
    return bool(
        _HEADING_PREFIX.match(value)
        or _SALUTATION.match(value)
        or uppercase_share >= 0.75
        or (len(words) >= 2 and title_share >= 0.8)
    )


def _collect_context(
    paragraphs: list[str],
    seed_index: int,
    direction: int,
    limit: int,
    selected_chars: int,
    max_chars: int,
) -> tuple[list[int], str, int]:
    indices = []
    current = seed_index + direction
    while len(indices) < limit and 0 <= current < len(paragraphs):
        text = paragraphs[current]
        if _looks_like_heading(text):
            return indices, "structural_boundary", selected_chars
        added = len(text) + 2
        if selected_chars + added > max_chars:
            return indices, "character_limit", selected_chars
        indices.append(current)
        selected_chars += added
        current += direction
    if not 0 <= current < len(paragraphs):
        reason = "document_start" if direction < 0 else "document_end"
    elif len(indices) >= limit:
        reason = "context_limit"
    else:
        reason = "complete"
    return indices, reason, selected_chars


def expand_story_window(
    paragraphs: list[str],
    seed_index: int,
    *,
    before: int = STORY_CONTEXT_BEFORE_PARAGRAPHS,
    after: int = STORY_CONTEXT_AFTER_PARAGRAPHS,
    max_paragraphs: int = PASSAGE_MAX_PARAGRAPHS,
    max_chars: int = PASSAGE_MAX_CHARS,
) -> StoryWindow:
    """Build a deterministic story window without reclassifying context."""
    if not 0 <= seed_index < len(paragraphs):
        raise IndexError("seed index is outside the document")
    if before < 0 or after < 0:
        raise ValueError("context paragraph limits cannot be negative")
    if max_paragraphs <= 0 or max_chars <= 0:
        raise ValueError("story limits must be positive")

    seed = paragraphs[seed_index]
    before_limit = min(before, max(0, max_paragraphs - 1))
    before_indices, before_reason, selected_chars = _collect_context(
        paragraphs,
        seed_index,
        -1,
        before_limit,
        len(seed),
        max_chars,
    )
    remaining = max(0, max_paragraphs - 1 - len(before_indices))
    after_limit = min(after, remaining)
    after_indices, after_reason, _selected_chars = _collect_context(
        paragraphs,
        seed_index,
        1,
        after_limit,
        selected_chars,
        max_chars,
    )
    ordered_indices = [*reversed(before_indices), seed_index, *after_indices]
    rows = []
    for paragraph_index in ordered_indices:
        role = (
            "seed"
            if paragraph_index == seed_index
            else "context_before"
            if paragraph_index < seed_index
            else "context_after"
        )
        rows.append(
            {
                "paragraph_index": paragraph_index,
                "role": role,
                "text": paragraphs[paragraph_index],
            }
        )
    text = "\n\n".join(row["text"] for row in rows)
    sentences = sentence_count(text)
    story_length_ready = (
        len(text) >= STORY_MIN_CHARS and sentences >= STORY_MIN_SENTENCES
    )
    fingerprint = hashlib.sha256(
        " ".join(text.split()).casefold().encode("utf-8")
    ).hexdigest()
    return StoryWindow(
        {
            "schema_version": 1,
            "expansion_version": STORY_EXPANSION_VERSION,
            "selection_policy": "precise_seed_with_unfiltered_document_context",
            "seed_paragraph_index": seed_index,
            "start_paragraph_index": ordered_indices[0],
            "end_paragraph_index": ordered_indices[-1],
            "paragraph_count": len(rows),
            "sentence_count": sentences,
            "character_count": len(text),
            "minimum_sentence_count": STORY_MIN_SENTENCES,
            "minimum_character_count": STORY_MIN_CHARS,
            "story_length_ready": story_length_ready,
            "readiness_basis": "minimum_characters_and_sentences",
            "boundary_before": before_reason,
            "boundary_after": after_reason,
            "story_fingerprint": fingerprint,
            "paragraphs": rows,
            "text": text,
        }
    )
