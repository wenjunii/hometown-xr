"""Integrity catalogs and research-quality products for enriched stories."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from config import OUTPUT_DIR, STORIES_DIR, STORY_EXPANSION_VERSION
from story_operations import (
    StoryFailureLedger,
    story_failure_ledger_path,
)

STORY_CATALOG_SCHEMA_VERSION = 1
STORY_QUALITY_SCHEMA_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_key(source_file: str) -> str:
    return hashlib.sha256(source_file.encode("utf-8")).hexdigest()[:20]


def _valid_story_row(row: dict) -> bool:
    story = row.get("story")
    if not isinstance(story, dict):
        return False
    paragraphs = story.get("paragraphs") if isinstance(story, dict) else None
    return bool(
        row.get("record_id")
        and row.get("source_file")
        and story.get("text")
        and story.get("story_fingerprint")
        and story.get("expansion_version")
        and isinstance(paragraphs, list)
        and sum(
            isinstance(paragraph, dict) and paragraph.get("role") == "seed"
            for paragraph in paragraphs
        )
        == 1
    )


def _current_story_row(row: dict) -> bool:
    return bool(
        _valid_story_row(row)
        and row["story"].get("expansion_version") == STORY_EXPANSION_VERSION
    )


def _read_fragment(path: Path) -> tuple[list[dict], list[str]]:
    rows = []
    errors = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"line {line_number}: invalid JSON ({exc.msg})")
                    continue
                if not isinstance(row, dict) or not _valid_story_row(row):
                    errors.append(f"line {line_number}: invalid story record")
                    continue
                rows.append(row)
    except (OSError, UnicodeError) as exc:
        errors.append(f"unreadable gzip fragment: {exc}")
    return rows, errors


def _write_gzip_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
    os.replace(temporary, path)


def _read_gzip_json(path: Path) -> dict | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.loads(handle.readline())
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None


def _catalog_path(stories_dir: str | Path) -> Path:
    return Path(stories_dir) / "_catalog.json.gz"


def build_story_catalog(stories_dir: str | Path = STORIES_DIR) -> dict:
    """Write a deterministic checksum catalog for every source fragment."""
    root = Path(stories_dir)
    entries = []
    integrity_errors = []
    for path in sorted((root / "_records").glob("*.jsonl.gz")):
        rows, errors = _read_fragment(path)
        source_files = sorted({str(row["source_file"]) for row in rows})
        relative = path.relative_to(root).as_posix()
        if errors:
            integrity_errors.append({"path": relative, "errors": errors})
        if len(source_files) != 1:
            integrity_errors.append(
                {
                    "path": relative,
                    "errors": [
                        f"expected one source_file, found {len(source_files)}"
                    ],
                }
            )
        source_file = source_files[0] if len(source_files) == 1 else ""
        if source_file and path.stem.split(".", 1)[0] != _source_key(source_file):
            integrity_errors.append(
                {
                    "path": relative,
                    "errors": ["fragment filename does not match source hash"],
                }
            )
        record_ids = [str(row["record_id"]) for row in rows]
        if len(record_ids) != len(set(record_ids)):
            integrity_errors.append(
                {
                    "path": relative,
                    "errors": ["fragment contains duplicate record IDs"],
                }
            )
        entries.append(
            {
                "path": relative,
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
                "records": len(rows),
                "current_records": sum(_current_story_row(row) for row in rows),
                "stale_records": sum(
                    not _current_story_row(row) for row in rows
                ),
                "source_file": source_file,
                "crawl_id": str(rows[0].get("crawl_id", "")) if rows else "",
                "record_ids": sorted(record_ids),
            }
        )
    entries_payload = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = {
        "schema_version": STORY_CATALOG_SCHEMA_VERSION,
        "expansion_version": STORY_EXPANSION_VERSION,
        "catalog_fingerprint": hashlib.sha256(entries_payload).hexdigest(),
        "fragments": len(entries),
        "records": sum(int(row["records"]) for row in entries),
        "current_records": sum(
            int(row["current_records"]) for row in entries
        ),
        "stale_records": sum(int(row["stale_records"]) for row in entries),
        "bytes": sum(int(row["bytes"]) for row in entries),
        "entries": entries,
    }
    if integrity_errors:
        return {
            "valid": False,
            "catalog_path": str(_catalog_path(root)),
            "integrity_errors": integrity_errors,
            **{key: value for key, value in payload.items() if key != "entries"},
        }
    target = _catalog_path(root)
    _write_gzip_json(target, payload)
    return {
        "valid": True,
        "catalog_path": str(target),
        "integrity_errors": [],
        **{key: value for key, value in payload.items() if key != "entries"},
    }


def verify_story_integrity(stories_dir: str | Path = STORIES_DIR) -> dict:
    """Verify catalog coverage, checksums, and source-fragment identities."""
    root = Path(stories_dir)
    target = _catalog_path(root)
    catalog = _read_gzip_json(target)
    actual = {
        path.relative_to(root).as_posix(): path
        for path in (root / "_records").glob("*.jsonl.gz")
    }
    if not catalog:
        return {
            "valid": not actual,
            "catalog_exists": False,
            "catalog_path": str(target),
            "fragments": len(actual),
            "records": 0,
            "missing_fragments": [],
            "uncovered_fragments": sorted(actual),
            "fragment_failures": [],
            "integrity_errors": len(actual),
        }

    expected = {
        str(entry["path"]): entry for entry in catalog.get("entries", [])
    }
    catalog_errors = []
    entries_payload = json.dumps(
        list(expected.values()),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(entries_payload).hexdigest() != catalog.get(
        "catalog_fingerprint"
    ):
        catalog_errors.append("catalog fingerprint is invalid")
    if catalog.get("expansion_version") != STORY_EXPANSION_VERSION:
        catalog_errors.append("catalog expansion version is stale")
    missing = sorted(set(expected) - set(actual))
    uncovered = sorted(set(actual) - set(expected))
    failures = []
    records = 0
    for relative, entry in sorted(expected.items()):
        path = actual.get(relative)
        if path is None:
            continue
        errors = []
        if path.stat().st_size != int(entry.get("bytes", -1)):
            errors.append("byte size differs from catalog")
        if _sha256(path) != entry.get("sha256"):
            errors.append("checksum differs from catalog")
        rows, row_errors = _read_fragment(path)
        errors.extend(row_errors)
        records += len(rows)
        if len(rows) != int(entry.get("records", -1)):
            errors.append("record count differs from catalog")
        source_files = {str(row["source_file"]) for row in rows}
        if source_files and source_files != {str(entry.get("source_file", ""))}:
            errors.append("source identity differs from catalog")
        if errors:
            failures.append({"path": relative, "errors": errors})
    error_count = (
        len(catalog_errors) + len(missing) + len(uncovered) + len(failures)
    )
    return {
        "valid": error_count == 0,
        "catalog_exists": True,
        "catalog_path": str(target),
        "catalog_fingerprint": catalog.get("catalog_fingerprint"),
        "fragments": len(actual),
        "records": records,
        "missing_fragments": missing,
        "uncovered_fragments": uncovered,
        "catalog_errors": catalog_errors,
        "fragment_failures": failures,
        "integrity_errors": error_count,
    }


def _story_rows(stories_dir: str | Path) -> list[dict]:
    rows = []
    for path in sorted((Path(stories_dir) / "_records").glob("*.jsonl.gz")):
        fragment_rows, _errors = _read_fragment(path)
        rows.extend(row for row in fragment_rows if _current_story_row(row))
    return rows


def story_gap_rows(
    stories_dir: str | Path = STORIES_DIR,
    output_dir: str | Path = OUTPUT_DIR,
) -> list[dict]:
    from story_enrichment import _load_source_groups

    groups = _load_source_groups(output_dir)
    existing_by_source: dict[str, set[str]] = defaultdict(set)
    for row in _story_rows(stories_dir):
        existing_by_source[str(row["source_file"])].add(str(row["record_id"]))
    failures = {
        str(row["source_file"]): row
        for row in StoryFailureLedger(
            story_failure_ledger_path(stories_dir)
        ).rows()
    }
    gaps = []
    for source_file, records in sorted(groups.items()):
        expected = {str(row["record_id"]) for row in records}
        present = existing_by_source.get(source_file, set())
        missing = sorted(expected - present)
        if not missing:
            continue
        failure = failures.get(source_file)
        gaps.append(
            {
                "source_file": source_file,
                "crawl_id": str(records[0].get("crawl_id", "")),
                "status": "partial" if present else "missing",
                "expected_matches": len(expected),
                "enriched_matches": len(expected & present),
                "missing_matches": len(missing),
                "missing_record_ids": missing,
                "retry": failure,
            }
        )
    return gaps


def build_story_quality_report(
    stories_dir: str | Path = STORIES_DIR,
    output_dir: str | Path = OUTPUT_DIR,
) -> dict:
    from story_enrichment import _group_story_records, _load_source_groups

    rows = _story_rows(stories_dir)
    fragment_errors = []
    stale_story_records = 0
    root = Path(stories_dir)
    for path in sorted((root / "_records").glob("*.jsonl.gz")):
        _fragment_rows, errors = _read_fragment(path)
        stale_story_records += sum(
            not _current_story_row(row) for row in _fragment_rows
        )
        if errors:
            fragment_errors.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "errors": errors,
                }
            )
    grouped = _group_story_records(rows)
    groups = _load_source_groups(output_dir)
    gaps = story_gap_rows(stories_dir, output_dir)
    domains = Counter()
    languages = Counter()
    signatures = Counter()
    exact_seed_mismatches = 0
    whitespace_collapsed_seed_mismatches = 0
    for row in rows:
        domain = urlparse(str(row.get("url", ""))).hostname or "unknown"
        domains[domain.lower()] += 1
        languages[str(row.get("language", "unknown"))] += 1
        signature = str(row.get("seed", {}).get("filter_signature", "unknown"))
        signatures[signature or "unknown"] += 1
        story = row["story"]
        seed_rows = [
            paragraph
            for paragraph in story.get("paragraphs", [])
            if paragraph.get("role") == "seed"
        ]
        source_seed = str(seed_rows[0].get("text", "")) if seed_rows else ""
        accepted_seed = str(row.get("seed", {}).get("paragraph", ""))
        if len(seed_rows) != 1 or source_seed != accepted_seed:
            exact_seed_mismatches += 1
        if len(seed_rows) != 1 or " ".join(source_seed.split()) != " ".join(
            accepted_seed.split()
        ):
            whitespace_collapsed_seed_mismatches += 1
    total_matches = sum(len(records) for records in groups.values())
    expected_record_ids = {
        str(record["record_id"])
        for records in groups.values()
        for record in records
    }
    enriched_record_ids = {
        str(row["record_id"]) for row in rows if str(row["record_id"]) in expected_record_ids
    }
    complete_sources = len(groups) - len(gaps)
    ready = sum(bool(row["story"].get("story_length_ready")) for row in grouped)
    top_domains = [
        {"domain": domain, "captures": count}
        for domain, count in domains.most_common(10)
    ]
    failure_summary = StoryFailureLedger(
        story_failure_ledger_path(stories_dir)
    ).summary()
    return {
        "schema_version": STORY_QUALITY_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "deterministic_extraction": True,
        "generated_text": False,
        "coverage": {
            "total_sources": len(groups),
            "complete_sources": complete_sources,
            "partial_or_missing_sources": len(gaps),
            "source_coverage": (
                round(complete_sources / len(groups), 6) if groups else 1.0
            ),
            "total_matches": total_matches,
            "enriched_matches": len(enriched_record_ids),
            "missing_matches": max(0, total_matches - len(enriched_record_ids)),
            "orphan_story_records": len(rows) - len(enriched_record_ids),
            "stale_story_records": stale_story_records,
            "match_coverage": (
                round(len(enriched_record_ids) / total_matches, 6)
                if total_matches
                else 1.0
            ),
        },
        "stories": {
            "source_captures": len(rows),
            "unique_stories": len(grouped),
            "duplicate_captures": max(0, len(rows) - len(grouped)),
            "story_length_ready": ready,
            "short_valid_context": len(grouped) - ready,
        },
        "verbatim_integrity": {
            "valid": (
                whitespace_collapsed_seed_mismatches == 0
                and not fragment_errors
            ),
            "exact_seed_paragraph_differences": exact_seed_mismatches,
            "whitespace_collapsed_seed_mismatches": (
                whitespace_collapsed_seed_mismatches
            ),
            "invalid_fragments": len(fragment_errors),
            "fragment_error_examples": fragment_errors[:10],
        },
        "languages": dict(sorted(languages.items())),
        "filter_signatures": dict(sorted(signatures.items())),
        "top_domains": top_domains,
        "maximum_domain_share": (
            round(top_domains[0]["captures"] / len(rows), 6)
            if rows and top_domains
            else 0.0
        ),
        "failures": failure_summary,
        "gap_sources": len(gaps),
    }


def write_story_quality_report(report: dict, export_dir: str | Path) -> dict:
    export_path = Path(export_dir)
    export_path.mkdir(parents=True, exist_ok=True)
    json_path = export_path / "story_quality_report.json"
    json_temporary = json_path.with_name(f"{json_path.name}.{os.getpid()}.tmp")
    json_temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(json_temporary, json_path)

    coverage = report["coverage"]
    stories = report["stories"]
    verbatim = report["verbatim_integrity"]
    markdown_path = export_path / "story_quality_report.md"
    markdown_temporary = markdown_path.with_name(
        f"{markdown_path.name}.{os.getpid()}.tmp"
    )
    lines = [
        "# Story Research Quality Report",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "## Coverage",
        "",
        f"- Complete sources: **{coverage['complete_sources']} / {coverage['total_sources']}**",
        f"- Enriched matches: **{coverage['enriched_matches']} / {coverage['total_matches']}**",
        f"- Partial or missing sources: **{coverage['partial_or_missing_sources']}**",
        "",
        "## Story Products",
        "",
        f"- Unique stories: **{stories['unique_stories']}**",
        f"- Story-length passages: **{stories['story_length_ready']}**",
        f"- Short valid context: **{stories['short_valid_context']}**",
        f"- Duplicate captures retained in provenance: **{stories['duplicate_captures']}**",
        "",
        "## Integrity",
        "",
        "- Deterministic extraction: **yes**",
        "- Generated text: **no**",
        "- Exact source/accepted seed differences: "
        f"**{verbatim['exact_seed_paragraph_differences']}**",
        "- Whitespace-collapsed source/accepted seed mismatches: "
        f"**{verbatim['whitespace_collapsed_seed_mismatches']}**",
        f"- Invalid story fragments: **{verbatim['invalid_fragments']}**",
        f"- Seed correspondence: **{'pass' if verbatim['valid'] else 'fail'}**",
        "",
        "## Operations",
        "",
        f"- Failure-ledger sources: **{report['failures']['sources']}**",
        f"- Cooldown sources: **{report['failures']['cooldown_sources']}**",
        f"- Quarantined sources: **{report['failures']['quarantined_sources']}**",
        "",
    ]
    markdown_temporary.write_text("\n".join(lines), encoding="utf-8")
    os.replace(markdown_temporary, markdown_path)
    return {
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
    }
