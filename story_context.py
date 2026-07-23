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
    STORY_REFERENCE_CONTEXT_PARAGRAPHS,
    STORY_REFERENCE_SCAN_PARAGRAPHS,
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
_LOSS_EVENT = re.compile(
    r"\b(?:death|died|dead|dying|loss|funeral|buried|passed away)\b",
    re.IGNORECASE,
)
_KINSHIP_PATTERNS = {
    "brother": re.compile(r"\bbrothers?(?:['\u2019]s)?\b", re.IGNORECASE),
    "sister": re.compile(r"\bsisters?(?:['\u2019]s)?\b", re.IGNORECASE),
    "mother": re.compile(r"\bmothers?(?:['\u2019]s)?\b", re.IGNORECASE),
    "father": re.compile(r"\bfathers?(?:['\u2019]s)?\b", re.IGNORECASE),
    "parent": re.compile(r"\bparents?(?:['\u2019]s)?\b", re.IGNORECASE),
    "child": re.compile(
        r"\b(?:child|children)(?:['\u2019]s)?\b",
        re.IGNORECASE,
    ),
    "son": re.compile(r"\bsons?(?:['\u2019]s)?\b", re.IGNORECASE),
    "daughter": re.compile(r"\bdaughters?(?:['\u2019]s)?\b", re.IGNORECASE),
    "grandmother": re.compile(
        r"\bgrandmothers?(?:['\u2019]s)?\b",
        re.IGNORECASE,
    ),
    "grandfather": re.compile(
        r"\bgrandfathers?(?:['\u2019]s)?\b",
        re.IGNORECASE,
    ),
    "grandparent": re.compile(
        r"\bgrandparents?(?:['\u2019]s)?\b",
        re.IGNORECASE,
    ),
    "husband": re.compile(r"\bhusbands?(?:['\u2019]s)?\b", re.IGNORECASE),
    "wife": re.compile(r"\bwi(?:fe|ves)(?:['\u2019]s)?\b", re.IGNORECASE),
}
_EMBEDDED_DOCUMENT_INTRO = re.compile(
    r"\b(?:following|enclosed|attached|received|responded|wrote)\b.*"
    r"\b(?:article|letter|message|email|report)\b",
    re.IGNORECASE,
)


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


def _starts_embedded_document(text: str) -> bool:
    value = " ".join(text.split()).strip()
    return value.endswith(":") and bool(_EMBEDDED_DOCUMENT_INTRO.search(value))


def _linked_loss_context(
    paragraphs: list[str],
    seed_index: int,
    *,
    scan_limit: int,
    after_limit: int,
    max_paragraphs: int,
    max_chars: int,
) -> tuple[list[int], dict | None]:
    """Find an earlier kinship-loss event explicitly referenced by the seed."""
    if max_paragraphs <= 0 or max_chars <= 0:
        return [], None
    seed = paragraphs[seed_index]
    if not _LOSS_EVENT.search(seed):
        return [], None
    kinship = [
        name for name, pattern in _KINSHIP_PATTERNS.items() if pattern.search(seed)
    ]
    if not kinship:
        return [], None

    lower_bound = max(0, seed_index - scan_limit)
    origin = None
    matched_kinship = None
    for index in range(seed_index - 1, lower_bound - 1, -1):
        text = paragraphs[index]
        if not _LOSS_EVENT.search(text):
            continue
        matched = next(
            (name for name in kinship if _KINSHIP_PATTERNS[name].search(text)),
            None,
        )
        if matched is not None:
            origin = index
            matched_kinship = matched
            break
    if origin is None:
        return [], None
    if len(paragraphs[origin]) > max_chars:
        return [], None

    indices = [origin]
    selected_chars = len(paragraphs[origin])
    current = origin + 1
    while (
        current < seed_index
        and len(indices) < max_paragraphs
        and len(indices) <= after_limit
    ):
        text = paragraphs[current]
        if _looks_like_heading(text) or _starts_embedded_document(text):
            break
        added = len(text) + 2
        if selected_chars + added > max_chars:
            break
        indices.append(current)
        selected_chars += added
        current += 1

    return indices, {
        "strategy": "kinship_loss_reference_v1",
        "origin_paragraph_index": origin,
        "matched_kinship": matched_kinship,
        "scan_start_paragraph_index": lower_bound,
    }


def _source_segments(indices: list[int], seed_index: int) -> tuple[list[dict], list[dict]]:
    segments = []
    start = indices[0]
    previous = start
    for index in indices[1:]:
        if index == previous + 1:
            previous = index
            continue
        segments.append((start, previous))
        start = previous = index
    segments.append((start, previous))

    segment_rows = [
        {
            "start_paragraph_index": first,
            "end_paragraph_index": last,
            "paragraph_count": last - first + 1,
            "contains_seed": first <= seed_index <= last,
        }
        for first, last in segments
    ]
    omissions = [
        {
            "after_paragraph_index": previous["end_paragraph_index"],
            "before_paragraph_index": current["start_paragraph_index"],
            "paragraph_count": (
                current["start_paragraph_index"]
                - previous["end_paragraph_index"]
                - 1
            ),
        }
        for previous, current in zip(segment_rows, segment_rows[1:])
    ]
    return segment_rows, omissions


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
    source_paragraphs: list[str] | None = None,
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
    if source_paragraphs is not None and len(source_paragraphs) != len(paragraphs):
        raise ValueError("source and normalized paragraphs must have the same length")

    seed = paragraphs[seed_index]
    source_values = source_paragraphs if source_paragraphs is not None else paragraphs
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
    if (
        after_reason == "structural_boundary"
        and after_indices
        and paragraphs[after_indices[-1]].rstrip().endswith(":")
    ):
        after_indices.pop()

    local_indices = [*reversed(before_indices), seed_index, *after_indices]
    local_chars = sum(len(paragraphs[index]) + 2 for index in local_indices)
    linked_indices, linked_context = _linked_loss_context(
        paragraphs,
        seed_index,
        scan_limit=STORY_REFERENCE_SCAN_PARAGRAPHS,
        after_limit=STORY_REFERENCE_CONTEXT_PARAGRAPHS,
        max_paragraphs=max(0, max_paragraphs - len(local_indices)),
        max_chars=max(0, max_chars - local_chars),
    )
    linked_indices = [index for index in linked_indices if index not in local_indices]
    if not linked_indices:
        linked_context = None
    ordered_indices = sorted([*linked_indices, *local_indices])
    segments, omissions = _source_segments(ordered_indices, seed_index)
    rows = []
    for paragraph_index in ordered_indices:
        role = (
            "seed"
            if paragraph_index == seed_index
            else "referenced_event"
            if linked_context
            and paragraph_index == linked_context["origin_paragraph_index"]
            else "referenced_context"
            if paragraph_index in linked_indices
            else "context_before"
            if paragraph_index < seed_index
            else "context_after"
        )
        source_text = source_values[paragraph_index]
        normalized_text = paragraphs[paragraph_index]
        row = {
            "paragraph_index": paragraph_index,
            "role": role,
            "text": source_text,
            "source_text_sha256": hashlib.sha256(
                source_text.encode("utf-8")
            ).hexdigest(),
        }
        if normalized_text != source_text:
            row["normalized_text"] = normalized_text
        rows.append(row)
    text = "\n\n".join(row["text"] for row in rows)
    normalized_text = "\n\n".join(paragraphs[index] for index in ordered_indices)
    sentences = sentence_count(normalized_text)
    story_length_ready = (
        len(normalized_text) >= STORY_MIN_CHARS
        and sentences >= STORY_MIN_SENTENCES
    )
    fingerprint = hashlib.sha256(
        " ".join(normalized_text.split()).casefold().encode("utf-8")
    ).hexdigest()
    return StoryWindow(
        {
            "schema_version": 1,
            "expansion_version": STORY_EXPANSION_VERSION,
            "selection_policy": (
                "precise_seed_with_deterministic_source_links"
                if linked_context
                else "precise_seed_with_unfiltered_document_context"
            ),
            "source_text_mode": (
                "verbatim_selected_source_paragraphs"
                if omissions
                else "verbatim_extracted_paragraphs"
            ),
            "seed_paragraph_index": seed_index,
            "start_paragraph_index": ordered_indices[0],
            "end_paragraph_index": ordered_indices[-1],
            "paragraph_count": len(rows),
            "segment_count": len(segments),
            "segments": segments,
            "omissions": omissions,
            "linked_context": linked_context,
            "sentence_count": sentences,
            "character_count": len(text),
            "normalized_character_count": len(normalized_text),
            "minimum_sentence_count": STORY_MIN_SENTENCES,
            "minimum_character_count": STORY_MIN_CHARS,
            "story_length_ready": story_length_ready,
            "readiness_basis": "minimum_characters_and_sentences",
            "boundary_before": before_reason,
            "boundary_after": after_reason,
            "story_fingerprint": fingerprint,
            "source_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "paragraphs": rows,
            "text": text,
        }
    )
