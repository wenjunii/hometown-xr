"""Build story-length records around precise matches without relaxing filters."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from config import OUTPUT_DIR, STORIES_DIR, STORY_EXPANSION_VERSION
from crawl_catalog import get_crawl_info
from downloader import stream_file
from output import OutputWriter
from processor import (
    ProcessingStats,
    extract_paragraphs_from_arc,
    extract_paragraphs_from_wet,
)
from record_identity import stable_record_id

STORY_RECORD_SCHEMA_VERSION = 1


def _source_key(source_file: str) -> str:
    return hashlib.sha256(source_file.encode("utf-8")).hexdigest()[:20]


def _fragment_path(source_file: str, stories_dir: str | Path = STORIES_DIR) -> Path:
    return Path(stories_dir) / "_records" / f"{_source_key(source_file)}.jsonl.gz"


def _write_gzip_rows(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as handle:
                for row in rows:
                    handle.write(
                        json.dumps(
                            row,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
    os.replace(temporary, path)


def _read_gzip_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []


def _read_manifest_records(writer: OutputWriter, manifest: dict) -> list[dict]:
    rows = []
    for shard in manifest.get("shards", []):
        path = writer.output_dir / str(shard["path"])
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            rows.extend(json.loads(line) for line in handle if line.strip())
    return rows


def _load_source_groups(
    output_dir: str | Path = OUTPUT_DIR,
    crawl_ids: set[str] | None = None,
    source_files: set[str] | None = None,
) -> dict[str, list[dict]]:
    writer = OutputWriter(output_dir)
    groups = {}
    for manifest in writer.iter_manifests():
        source_file = str(manifest["source_file"])
        if source_files is not None and source_file not in source_files:
            continue
        records = _read_manifest_records(writer, manifest)
        if not records:
            continue
        if crawl_ids is not None and str(records[0].get("crawl_id", "")) not in crawl_ids:
            continue
        groups[source_file] = records
    return groups


def _valid_story(story: object) -> bool:
    if not isinstance(story, dict):
        return False
    paragraphs = story.get("paragraphs")
    return bool(
        story.get("text")
        and story.get("story_fingerprint")
        and story.get("expansion_version") == STORY_EXPANSION_VERSION
        and isinstance(paragraphs, list)
        and all(isinstance(row, dict) for row in paragraphs)
        and sum(row.get("role") == "seed" for row in paragraphs) == 1
    )


def build_enriched_story(match_record: dict, story: dict) -> dict:
    """Pair a precise seed decision with role-labeled unfiltered context."""
    if not _valid_story(story):
        raise ValueError("story payload is incomplete")
    record_id = str(match_record["record_id"])
    return {
        "schema_version": STORY_RECORD_SCHEMA_VERSION,
        "story_id": str(story["story_fingerprint"]),
        "record_id": record_id,
        "crawl_id": str(match_record.get("crawl_id", "")),
        "source_file": str(match_record.get("source_file", "")),
        "url": str(match_record.get("url", "")),
        "warc_date": str(match_record.get("warc_date", "")),
        "language": str(match_record.get("language", "unknown")),
        "language_confidence": float(match_record.get("language_confidence", 0.0)),
        "seed": {
            "paragraph": str(match_record.get("paragraph", "")),
            "matched_keywords": list(match_record.get("matched_keywords", [])),
            "semantic_score": float(match_record.get("semantic_score", 0.0)),
            "concept_match": str(match_record.get("concept_match", "")),
            "narrative_score": int(match_record.get("narrative_score", 0) or 0),
            "filter_signature": str(match_record.get("filter_signature", "")),
        },
        "story": story,
    }


def _normalized_match_key(row: dict | object) -> tuple[str, str, str]:
    if isinstance(row, dict):
        url = str(row.get("url", ""))
        warc_date = str(row.get("warc_date", ""))
        text = str(row.get("paragraph", ""))
    else:
        url = str(row.url)
        warc_date = str(row.warc_date)
        text = str(row.text)
    return url, warc_date, " ".join(text.split())


def _recover_missing_stories(
    source_file: str,
    records: list[dict],
) -> tuple[dict[str, dict], dict]:
    crawl_id = str(records[0].get("crawl_id", ""))
    crawl_info = get_crawl_info(crawl_id)
    targets = {str(record["record_id"]): record for record in records}
    targets_by_key: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for record in records:
        targets_by_key[_normalized_match_key(record)].append(record)
    found = {}
    stats = ProcessingStats()
    with stream_file(source_file, crawl_info) as stream:
        extractor = (
            extract_paragraphs_from_arc
            if crawl_info.era == "legacy"
            else extract_paragraphs_from_wet
        )
        for paragraph, _keywords, _records_seen in extractor(
            stream,
            crawl_id,
            keyword_matcher=None,
            stats=stats,
            source_file=source_file,
            include_unmatched=True,
        ):
            parsed_id = stable_record_id(
                paragraph.crawl_id,
                source_file,
                paragraph.url,
                paragraph.warc_date,
                paragraph.text,
            )
            record = targets.get(parsed_id)
            if record is None:
                candidates = targets_by_key.get(_normalized_match_key(paragraph), [])
                record = next(
                    (
                        candidate
                        for candidate in candidates
                        if str(candidate["record_id"]) not in found
                    ),
                    None,
                )
            if record is not None and _valid_story(paragraph.story):
                found[str(record["record_id"])] = paragraph.story
                if len(found) == len(targets):
                    break
    return found, {
        "records_processed": stats.records_processed,
        "eligible_paragraphs": stats.eligible_paragraphs,
    }


def _fragment_record_ids(path: Path) -> set[str]:
    return {
        str(row.get("record_id", ""))
        for row in _read_gzip_rows(path)
        if _valid_story(row.get("story"))
    }


def plan_story_enrichment(
    output_dir: str | Path = OUTPUT_DIR,
    stories_dir: str | Path = STORIES_DIR,
    crawl_ids: Iterable[str] | None = None,
    source_files: Iterable[str] | None = None,
    limit: int | None = None,
) -> dict:
    """Plan pending source enrichment without downloading Common Crawl data."""
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    groups = _load_source_groups(
        output_dir,
        set(crawl_ids) if crawl_ids else None,
        set(source_files) if source_files else None,
    )
    pending = []
    complete_sources = 0
    complete_matches = 0
    for source_file, records in sorted(groups.items()):
        expected = {str(record["record_id"]) for record in records}
        completed = _fragment_record_ids(_fragment_path(source_file, stories_dir))
        missing = sorted(expected - completed)
        if missing:
            pending.append(
                {
                    "source_file": source_file,
                    "crawl_id": str(records[0].get("crawl_id", "")),
                    "matches": len(records),
                    "missing_matches": len(missing),
                }
            )
        else:
            complete_sources += 1
            complete_matches += len(records)
    selected = pending[:limit] if limit is not None else pending
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(output_dir),
        "stories_dir": str(stories_dir),
        "total_sources": len(groups),
        "total_matches": sum(len(records) for records in groups.values()),
        "complete_sources": complete_sources,
        "complete_matches": complete_matches,
        "pending_sources": len(pending),
        "pending_matches": sum(row["missing_matches"] for row in pending),
        "selected_sources": len(selected),
        "selection": selected,
    }


def enrich_story_sources(
    output_dir: str | Path = OUTPUT_DIR,
    stories_dir: str | Path = STORIES_DIR,
    crawl_ids: Iterable[str] | None = None,
    source_files: Iterable[str] | None = None,
    limit: int | None = None,
) -> dict:
    """Enrich a resumable source batch and leave canonical match output untouched."""
    plan = plan_story_enrichment(
        output_dir,
        stories_dir,
        crawl_ids=crawl_ids,
        source_files=source_files,
        limit=limit,
    )
    selected_files = {row["source_file"] for row in plan["selection"]}
    groups = _load_source_groups(output_dir, source_files=selected_files)
    results = []
    for source_file in sorted(selected_files):
        records = groups[source_file]
        path = _fragment_path(source_file, stories_dir)
        records_by_id = {str(record["record_id"]): record for record in records}
        existing = {
            str(row["record_id"]): row
            for row in _read_gzip_rows(path)
            if (
                str(row.get("record_id", "")) in records_by_id
                and _valid_story(row.get("story"))
            )
        }
        missing_records = [
            record for record in records if str(record["record_id"]) not in existing
        ]
        recovered = {}
        embedded = 0
        for record in missing_records:
            if _valid_story(record.get("story")):
                recovered[str(record["record_id"])] = record["story"]
                embedded += 1
        unresolved_records = [
            record
            for record in missing_records
            if str(record["record_id"]) not in recovered
        ]
        parse_stats = {"records_processed": 0, "eligible_paragraphs": 0}
        error = None
        if unresolved_records:
            try:
                parsed, parse_stats = _recover_missing_stories(
                    source_file,
                    unresolved_records,
                )
                recovered.update(parsed)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        for record_id, story in recovered.items():
            existing[record_id] = build_enriched_story(records_by_id[record_id], story)
        if existing:
            _write_gzip_rows(
                path,
                [existing[record_id] for record_id in sorted(existing)],
            )
        expected_ids = set(records_by_id)
        missing_ids = sorted(expected_ids - set(existing))
        results.append(
            {
                "source_file": source_file,
                "crawl_id": str(records[0].get("crawl_id", "")),
                "status": (
                    "completed"
                    if not missing_ids
                    else "partial"
                    if existing
                    else "failed"
                ),
                "matches": len(records),
                "stories": len(expected_ids & set(existing)),
                "embedded_stories": embedded,
                "recovered_stories": len(recovered) - embedded,
                "missing_record_ids": missing_ids,
                "error": error,
                **parse_stats,
            }
        )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "preserves_match_output": True,
        "plan": plan,
        "completed_sources": sum(row["status"] == "completed" for row in results),
        "partial_sources": sum(row["status"] == "partial" for row in results),
        "failed_sources": sum(row["status"] == "failed" for row in results),
        "stories_written": sum(row["stories"] for row in results),
        "sources": results,
    }


def iter_story_records(stories_dir: str | Path = STORIES_DIR):
    for path in sorted((Path(stories_dir) / "_records").glob("*.jsonl.gz")):
        for row in _read_gzip_rows(path):
            if _valid_story(row.get("story")):
                yield row


def _group_story_records(rows: Iterable[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row["story_id"])].append(row)
    stories = []
    for story_id, captures in grouped.items():
        captures.sort(
            key=lambda row: (
                -float(row["seed"].get("semantic_score", 0.0)),
                str(row.get("warc_date", "")),
                str(row.get("record_id", "")),
            )
        )
        representative = captures[0]
        stories.append(
            {
                **representative,
                "story_id": story_id,
                "capture_count": len(captures),
                "captures": [
                    {
                        "record_id": row["record_id"],
                        "crawl_id": row["crawl_id"],
                        "source_file": row["source_file"],
                        "url": row["url"],
                        "warc_date": row["warc_date"],
                    }
                    for row in captures
                ],
            }
        )
    stories.sort(
        key=lambda row: (
            -float(row["seed"].get("semantic_score", 0.0)),
            row["story_id"],
        )
    )
    return stories


def export_stories(
    stories_dir: str | Path = STORIES_DIR,
    export_dir: str | Path = STORIES_DIR.parent / "exports",
) -> dict:
    """Write deterministic structured and Markdown story exports."""
    export_path = Path(export_dir)
    export_path.mkdir(parents=True, exist_ok=True)
    stories = _group_story_records(iter_story_records(stories_dir))
    structured_path = export_path / "stories.jsonl.gz"
    _write_gzip_rows(structured_path, stories)
    by_language: dict[str, list[dict]] = defaultdict(list)
    for story in stories:
        by_language[str(story.get("language", "unknown"))].append(story)
    generated = set()
    for language, rows in sorted(by_language.items()):
        safe_language = "".join(
            character if character.isalnum() or character in "_-" else "_"
            for character in language
        ) or "unknown"
        destination = export_path / f"stories_{safe_language}.md"
        temporary = destination.with_suffix(".md.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write("# Expanded Home and Belonging Stories\n\n")
            handle.write(f"**Language:** `{language}`\n")
            handle.write(f"**Unique Stories:** {len(rows)}\n\n---\n\n")
            for index, row in enumerate(rows, 1):
                seed = row["seed"]
                story = row["story"]
                handle.write(
                    f"### {index}. Seed Score: "
                    f"{float(seed.get('semantic_score', 0.0)):.3f}\n"
                )
                handle.write(
                    "- **Story-Length Context:** `"
                    + ("yes" if story.get("story_length_ready") else "no")
                    + "`\n"
                )
                handle.write(
                    f"- **Story Size:** {story.get('paragraph_count', 0)} paragraphs, "
                    f"{story.get('sentence_count', 0)} sentences\n"
                )
                handle.write(
                    "- **Keywords:** `"
                    + ", ".join(seed.get("matched_keywords", []))
                    + "`\n"
                )
                handle.write(
                    f"- **Concept Anchor:** '{seed.get('concept_match', '')}'\n"
                )
                handle.write(f"- **Source URL:** [{row['url']}]({row['url']})\n")
                handle.write(f"- **Capture Count:** {row['capture_count']}\n")
                handle.write(f"- **Crawl Dataset:** `{row['crawl_id']}`\n")
                handle.write(f"- **Source File:** `{row['source_file']}`\n\n")
                for paragraph in story["paragraphs"]:
                    role = paragraph["role"].replace("_", " ").title()
                    handle.write(f"**{role}:**\n\n")
                    handle.write(
                        "\n".join(
                            f"> {line}" for line in str(paragraph["text"]).splitlines()
                        )
                    )
                    handle.write("\n\n")
                handle.write("---\n" if index == len(rows) else "---\n\n")
        os.replace(temporary, destination)
        generated.add(destination)
    for old_export in export_path.glob("stories_*.md"):
        if old_export not in generated:
            old_export.unlink()
    return {
        "schema_version": 1,
        "unique_stories": len(stories),
        "source_captures": sum(row["capture_count"] for row in stories),
        "story_length_ready": sum(
            bool(row["story"].get("story_length_ready")) for row in stories
        ),
        "languages": {language: len(rows) for language, rows in sorted(by_language.items())},
        "structured_path": str(structured_path),
        "markdown_paths": [str(path) for path in sorted(generated)],
    }
