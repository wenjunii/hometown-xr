"""Assemble adjacent accepted paragraphs and extract explainable metadata."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Callable, Iterable

from config import OUTPUT_SCHEMA_VERSION, PASSAGE_MAX_CHARS, PASSAGE_MAX_PARAGRAPHS
from quality import boilerplate_features, boilerplate_score, classify_content

_YEAR = re.compile(r"\b(?:18|19|20)\d{2}\b")
_DECADE = re.compile(r"\b(?:18|19|20)\d0s\b", re.IGNORECASE)
_TIME_PHRASES = re.compile(
    r"\b(?:when I was (?:a child|young)|during my childhood|"
    r"years? ago|months? ago|after \d+ years?|before I left|"
    r"when we moved|growing up|in my youth)\b",
    re.IGNORECASE,
)
_PLACE = re.compile(
    r"\b(?:[Bb]orn|[Rr]aised|[Gg]rew up|[Ll]ived|[Ss]tayed|"
    r"[Rr]eturned|[Mm]oved|[Cc]ame|[Ll]eft)"
    r"\s+(?:in|at|near|to|from)\s+"
    r"([A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){0,4})",
)
_ROUTE = re.compile(
    r"\b(?:[Mm]oved|[Mm]igrated|[Ee]migrated|[Tt]ravelled|"
    r"[Tt]raveled|[Cc]ame|[Ww]ent|[Ff]led)?\s*"
    r"[Ff]rom\s+([A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){0,3})"
    r"\s+[Tt]o\s+([A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){0,3})",
)


def extract_story_metadata(text: str, warc_date: str = "") -> dict:
    """Extract conservative place and time candidates with explicit confidence."""
    years = sorted({int(value) for value in _YEAR.findall(text)})
    decades = sorted({value.lower() for value in _DECADE.findall(text)})
    time_expressions = sorted(
        {match.group(0) for match in _TIME_PHRASES.finditer(text)},
        key=str.casefold,
    )
    places = []
    for match in _PLACE.finditer(text):
        value = match.group(1).strip(" ,.;:!?")
        if len(value) >= 2 and value.casefold() not in {"home", "school", "town"}:
            places.append(value)
    routes = []
    for match in _ROUTE.finditer(text):
        origin = match.group(1).strip(" ,.;:!?")
        destination = match.group(2).strip(" ,.;:!?")
        if origin.casefold() != destination.casefold():
            routes.append(f"{origin} -> {destination}")
            places.extend((origin, destination))
    places = list(dict.fromkeys(places))
    routes = list(dict.fromkeys(routes))
    capture_year = None
    capture_match = _YEAR.search(warc_date or "")
    if capture_match:
        capture_year = int(capture_match.group(0))
    evidence_types = sum(
        bool(value) for value in (places, years, decades, time_expressions, routes)
    )
    confidence = min(
        0.95,
        0.25
        + 0.15 * evidence_types
        + 0.1 * bool(routes)
        + 0.05 * (len(years) > 1),
    )
    return {
        "place_mentions": places,
        "year_mentions": years,
        "decade_mentions": decades,
        "time_expressions": time_expressions,
        "migration_routes": routes,
        "capture_year": capture_year,
        "metadata_confidence": round(confidence, 4) if evidence_types else 0.0,
        "metadata_method": "regex-v1",
    }


def _passage_id(records: list[dict]) -> str:
    identity = "\0".join(
        [
            str(records[0].get("document_id", "")),
            str(records[0].get("paragraph_index", 0)),
            str(records[-1].get("paragraph_index", 0)),
            *[str(record.get("record_id", "")) for record in records],
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def build_passage(records: list[dict]) -> dict:
    """Build one passage record from adjacent paragraph-level stories."""
    if not records:
        raise ValueError("at least one record is required")
    ordered = sorted(records, key=lambda row: int(row.get("paragraph_index", 0)))
    text = "\n\n".join(str(row.get("paragraph", "")) for row in ordered)
    classification = classify_content(text, str(ordered[0].get("url", "")))
    features = boilerplate_features(text)
    languages = Counter(str(row.get("language", "unknown")) for row in ordered)
    language = languages.most_common(1)[0][0]
    keyword_values = sorted(
        {
            str(keyword)
            for row in ordered
            for keyword in row.get("matched_keywords", [])
        }
    )
    semantic_scores = [float(row.get("semantic_score", 0.0)) for row in ordered]
    narrative_scores = [int(row.get("narrative_score", 0)) for row in ordered]
    metadata = extract_story_metadata(text, str(ordered[0].get("warc_date", "")))
    curated_default = (
        classification.category == "personal_prose"
        and boilerplate_score(features) < 4
        and all(bool(row.get("within_domain_cap")) for row in ordered)
    )
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "passage_id": _passage_id(ordered),
        "record_ids": [str(row.get("record_id", "")) for row in ordered],
        "story_ids": [str(row.get("story_id", "")) for row in ordered],
        "crawl_id": str(ordered[0].get("crawl_id", "")),
        "source_file": str(ordered[0].get("source_file", "")),
        "run_id": str(ordered[0].get("run_id", "")),
        "filter_signature": str(ordered[0].get("filter_signature", "")),
        "url": str(ordered[0].get("url", "")),
        "domain": str(ordered[0].get("domain", "unknown")),
        "warc_date": str(ordered[0].get("warc_date", "")),
        "language": language,
        "language_confidence": max(
            float(row.get("language_confidence", 0.0)) for row in ordered
        ),
        "document_id": str(ordered[0].get("document_id", "")),
        "start_paragraph_index": int(ordered[0].get("paragraph_index", 0)),
        "end_paragraph_index": int(ordered[-1].get("paragraph_index", 0)),
        "paragraph_count": len(ordered),
        "paragraph": text,
        "context_before": str(ordered[0].get("context_before", "")),
        "context_after": str(ordered[-1].get("context_after", "")),
        "matched_keywords": keyword_values,
        "semantic_score_max": max(semantic_scores),
        "semantic_score_mean": sum(semantic_scores) / len(semantic_scores),
        "narrative_score_max": max(narrative_scores),
        "content_category": classification.category,
        "content_confidence": classification.confidence,
        "content_flags": list(classification.flags),
        "content_reasons": list(classification.reasons),
        "curated_default": curated_default,
        **metadata,
    }


def assemble_story_passages(
    records: Iterable[dict],
    max_paragraphs: int = PASSAGE_MAX_PARAGRAPHS,
    max_chars: int = PASSAGE_MAX_CHARS,
) -> list[dict]:
    """Assemble adjacent records without crossing document or language boundaries."""
    if max_paragraphs <= 0 or max_chars <= 0:
        raise ValueError("passage limits must be positive")
    result = []
    current: list[dict] = []
    current_chars = 0
    for record in records:
        text = str(record.get("paragraph", ""))
        adjacent = (
            current
            and record.get("document_id")
            and record.get("document_id") == current[-1].get("document_id")
            and record.get("language") == current[-1].get("language")
            and int(record.get("paragraph_index", 0))
            == int(current[-1].get("paragraph_index", 0)) + 1
        )
        fits = len(current) < max_paragraphs and current_chars + len(text) <= max_chars
        if current and (not adjacent or not fits):
            result.append(build_passage(current))
            current = []
            current_chars = 0
        current.append(record)
        current_chars += len(text)
    if current:
        result.append(build_passage(current))
    return result


class PassageAssembler:
    """Streaming document assembler used by the Parquet exporter."""

    def __init__(self, emit: Callable[[dict], None]):
        self.emit = emit
        self.current: list[dict] = []

    def observe(self, record: dict) -> None:
        if self.current:
            previous = self.current[-1]
            adjacent = (
                record.get("document_id")
                and record.get("document_id") == previous.get("document_id")
                and record.get("language") == previous.get("language")
                and int(record.get("paragraph_index", 0))
                == int(previous.get("paragraph_index", 0)) + 1
            )
            chars = sum(len(str(row.get("paragraph", ""))) for row in self.current)
            fits = (
                len(self.current) < PASSAGE_MAX_PARAGRAPHS
                and chars + len(str(record.get("paragraph", ""))) <= PASSAGE_MAX_CHARS
            )
            if not adjacent or not fits:
                self.flush()
        self.current.append(record)

    def flush(self) -> None:
        if self.current:
            self.emit(build_passage(self.current))
            self.current = []
