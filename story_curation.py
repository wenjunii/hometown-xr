"""Deterministic story quality ranking and near-duplicate clustering."""

from __future__ import annotations

import io
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

from config import DATA_DIR
from deterministic_gzip import gzip_binary_writer
from quality import boilerplate_features, boilerplate_score, classify_content
from record_identity import hamming_distance, simhash64, text_fingerprint

DEFAULT_STORY_EXPORT = DATA_DIR / "exports" / "stories_complete.jsonl.gz"
DEFAULT_CURATED_EXPORT = DATA_DIR / "exports" / "stories_curated.jsonl.gz"
DEFAULT_CURATION_REPORT = DATA_DIR / "exports" / "story_curation_report.json"


def _story_text(row: dict) -> str:
    story = row.get("story") or {}
    if story.get("text"):
        return str(story["text"])
    return "\n\n".join(
        str(paragraph.get("text", ""))
        for paragraph in story.get("paragraphs", [])
        if paragraph.get("text")
    )


def assess_story(row: dict) -> dict:
    """Score source completeness and quality without changing source text."""
    story = row.get("story") or {}
    seed = row.get("seed") or {}
    text = _story_text(row)
    features = boilerplate_features(text)
    boilerplate = boilerplate_score(features)
    classification = classify_content(text, str(row.get("url", "")))
    paragraph_count = int(story.get("paragraph_count") or 0)
    sentence_count = int(story.get("sentence_count") or 0)
    segment_count = max(1, int(story.get("segment_count") or 1))
    omitted = sum(int(item.get("paragraph_count") or 0) for item in story.get("omissions") or [])
    semantic = max(0.0, min(1.0, float(seed.get("semantic_score") or 0.0)))
    narrative = max(0.0, min(1.0, float(seed.get("narrative_score") or 0) / 16))
    completeness = (
        0.4 * bool(story.get("story_length_ready"))
        + 0.3 * min(1.0, paragraph_count / 4)
        + 0.3 * min(1.0, sentence_count / 8)
    )
    continuity = max(0.0, 1.0 - 0.18 * (segment_count - 1) - 0.002 * omitted)
    cleanliness = max(0.0, 1.0 - boilerplate / 8)
    quality = (
        0.25 * semantic
        + 0.25 * narrative
        + 0.2 * completeness
        + 0.2 * continuity
        + 0.1 * cleanliness
    )
    warnings = list(features)
    if not story.get("story_length_ready"):
        warnings.append("short_context")
    if segment_count > 1:
        warnings.append("non_contiguous_source_segments")
    if classification.category != "personal_prose":
        warnings.append(f"content_{classification.category}")
        quality *= 0.5
    eligible = (
        classification.category == "personal_prose"
        and bool(story.get("story_length_ready"))
        and boilerplate < 4
    )
    return {
        "schema_version": 1,
        "quality_score": round(quality, 6),
        "eligible_default": eligible,
        "semantic_component": round(semantic, 6),
        "narrative_component": round(narrative, 6),
        "completeness_component": round(completeness, 6),
        "continuity_component": round(continuity, 6),
        "cleanliness_component": round(cleanliness, 6),
        "content_category": classification.category,
        "content_confidence": round(classification.confidence, 4),
        "boilerplate_score": boilerplate,
        "warnings": sorted(set(warnings)),
        "source_text_preserved": True,
        "generated_text": False,
    }


def _bands(value: int):
    for index in range(4):
        yield index, (value >> (index * 16)) & 0xFFFF


def curate_story_rows(rows: list[dict], near_distance: int = 3) -> dict:
    """Rank stories and identify duplicates while retaining every capture."""
    if not 0 <= near_distance <= 64:
        raise ValueError("near distance must be between 0 and 64")
    assessed = [{**row, "curation": assess_story(row)} for row in rows]
    assessed.sort(
        key=lambda row: (
            -float(row["curation"]["quality_score"]),
            str(row.get("story_id", "")),
        )
    )
    exact: dict[str, str] = {}
    hashes: dict[str, int] = {}
    bands: dict[tuple[int, int], list[str]] = defaultdict(list)
    duplicate_counts = Counter()
    for rank, row in enumerate(assessed, start=1):
        story_id = str(row.get("story_id", ""))
        text = _story_text(row)
        exact_key = text_fingerprint(text)
        fingerprint = simhash64(text)
        duplicate = None
        if exact_key in exact:
            duplicate = {
                "kind": "exact",
                "canonical_story_id": exact[exact_key],
                "distance": 0,
            }
        else:
            candidate_ids = {
                candidate for band in _bands(fingerprint) for candidate in bands.get(band, [])
            }
            nearest = min(
                (
                    (hamming_distance(fingerprint, hashes[candidate]), candidate)
                    for candidate in candidate_ids
                ),
                default=None,
            )
            if nearest is not None and nearest[0] <= near_distance:
                duplicate = {
                    "kind": "near",
                    "canonical_story_id": nearest[1],
                    "distance": nearest[0],
                }
        if duplicate is None:
            exact[exact_key] = story_id
            hashes[story_id] = fingerprint
            for band in _bands(fingerprint):
                bands[band].append(story_id)
            duplicate = {
                "kind": "canonical",
                "canonical_story_id": story_id,
                "distance": 0,
            }
        duplicate_counts[duplicate["kind"]] += 1
        row["curation"] = {
            **row["curation"],
            "rank": rank,
            "duplicate": duplicate,
        }
    return {
        "schema_version": 1,
        "deterministic": True,
        "source_text_preserved": True,
        "generated_text": False,
        "near_distance": near_distance,
        "stories": len(assessed),
        "canonical_stories": duplicate_counts["canonical"],
        "exact_duplicates": duplicate_counts["exact"],
        "near_duplicates": duplicate_counts["near"],
        "eligible_default": sum(
            bool(row["curation"]["eligible_default"])
            for row in assessed
            if row["curation"]["duplicate"]["kind"] == "canonical"
        ),
        "rows": assessed,
    }


def write_story_curation(
    source_path: str | Path = DEFAULT_STORY_EXPORT,
    output_path: str | Path = DEFAULT_CURATED_EXPORT,
    report_path: str | Path = DEFAULT_CURATION_REPORT,
    near_distance: int = 3,
) -> dict:
    """Write a ranked exact-source dataset plus a compact quality report."""
    from story_review import load_story_export

    result = curate_story_rows(load_story_export(source_path), near_distance)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("wb") as raw:
        with gzip_binary_writer(raw) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as handle:
                for row in result["rows"]:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, output)
    summary = {key: value for key, value in result.items() if key != "rows"}
    report = Path(report_path)
    report.parent.mkdir(parents=True, exist_ok=True)
    report_temporary = report.with_suffix(report.suffix + ".tmp")
    report_temporary.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(report_temporary, report)
    markdown = report.with_suffix(".md")
    markdown_temporary = markdown.with_suffix(markdown.suffix + ".tmp")
    markdown_temporary.write_text(
        "\n".join(
            [
                "# Deterministic Story Curation",
                "",
                "- Source text preserved: **yes**",
                "- Generated text: **no**",
                f"- Ranked stories: **{summary['stories']}**",
                f"- Canonical stories: **{summary['canonical_stories']}**",
                f"- Exact duplicates: **{summary['exact_duplicates']}**",
                f"- Near duplicates: **{summary['near_duplicates']}**",
                f"- Default eligible: **{summary['eligible_default']}**",
                "",
                "The quality score ranks review work. It never deletes, rewrites, or "
                "paraphrases a source story.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.replace(markdown_temporary, markdown)
    return {
        **summary,
        "output_path": str(output),
        "report_path": str(report),
        "markdown_path": str(markdown),
    }
