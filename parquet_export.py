"""Atomic canonical-story and capture-provenance Parquet export."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config import (
    DOMAIN_SHARE_WARNING,
    DOMAIN_STORY_CAP,
    OUTPUT_DIR,
    OUTPUT_SCHEMA_VERSION,
    PARQUET_DIR,
)
from dedupe import DedupIndex
from evaluation import iter_output_records
from quality import (
    DiversityTracker,
    boilerplate_features,
    boilerplate_score,
    concept_cluster_id,
    domain_from_url,
    template_fingerprint,
)
from record_identity import (
    content_fingerprint,
    stable_record_id,
    story_fingerprint,
)

_SAFE_PARTITION = re.compile(r"[^A-Za-z0-9._-]+")


def _partition_value(value: str) -> str:
    return _SAFE_PARTITION.sub("_", value or "unknown")[:120] or "unknown"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_with_identity(record: dict) -> dict:
    paragraph = str(record.get("paragraph", ""))
    source_file = str(record.get("source_file", ""))
    record_id = record.get("record_id") or stable_record_id(
        str(record.get("crawl_id", "")),
        source_file,
        str(record.get("url", "")),
        str(record.get("warc_date", "")),
        paragraph,
    )
    return {
        "schema_version": int(record.get("schema_version", OUTPUT_SCHEMA_VERSION)),
        "record_id": str(record_id),
        "content_fingerprint": str(
            record.get("content_fingerprint")
            or content_fingerprint(str(record.get("url", "")), paragraph)
        ),
        "story_fingerprint": story_fingerprint(paragraph),
        "crawl_id": str(record.get("crawl_id", "") or "unknown"),
        "source_file": source_file,
        "url": str(record.get("url", "")),
        "warc_date": str(record.get("warc_date", "")),
        "language": str(record.get("language", "unknown") or "unknown"),
        "language_confidence": float(record.get("language_confidence", 0.0) or 0.0),
        "paragraph": paragraph,
        "matched_keywords": [str(value) for value in record.get("matched_keywords", [])],
        "semantic_score": float(record.get("semantic_score", 0.0) or 0.0),
        "concept_match": str(record.get("concept_match", "")),
        "narrative_score": int(record.get("narrative_score", 0) or 0),
    }


class _PartitionedWriter:
    def __init__(self, root: Path, schema, partition_fields: tuple[str, ...], batch_size: int):
        self.root = root
        self.schema = schema
        self.partition_fields = partition_fields
        self.batch_size = batch_size
        self.buffers: dict[tuple[str, ...], list[dict]] = defaultdict(list)
        self.part_numbers: dict[tuple[str, ...], int] = defaultdict(int)
        self.partition_rows: dict[str, int] = defaultdict(int)
        self.rows = 0
        self.buffered_rows = 0

    def add(self, record: dict) -> None:
        key = tuple(str(record.get(field, "unknown")) for field in self.partition_fields)
        self.buffers[key].append(record)
        self.buffered_rows += 1
        if len(self.buffers[key]) >= self.batch_size:
            self.flush(key)

    def flush(self, key: tuple[str, ...]) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        records = self.buffers[key]
        if not records:
            return
        relative_dir = Path(
            *[
                f"{field}={_partition_value(value)}"
                for field, value in zip(self.partition_fields, key)
            ]
        )
        part_number = self.part_numbers[key]
        self.part_numbers[key] += 1
        path = self.root / relative_dir / f"part-{part_number:05d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.Table.from_pylist(records, schema=self.schema),
            path,
            compression="zstd",
        )
        count = len(records)
        self.partition_rows[relative_dir.as_posix()] += count
        self.rows += count
        self.buffered_rows -= count
        self.buffers[key] = []

    def flush_largest(self) -> None:
        candidates = [key for key, rows in self.buffers.items() if rows]
        if candidates:
            self.flush(max(candidates, key=lambda key: len(self.buffers[key])))

    def flush_all(self) -> None:
        for key in list(self.buffers):
            self.flush(key)


def _schemas():
    import pyarrow as pa

    stories = pa.schema(
        [
            ("schema_version", pa.int16()),
            ("story_id", pa.string()),
            ("record_id", pa.string()),
            ("story_fingerprint", pa.string()),
            ("content_fingerprint", pa.string()),
            ("crawl_id", pa.string()),
            ("source_file", pa.string()),
            ("url", pa.string()),
            ("domain", pa.string()),
            ("domain_story_rank", pa.int32()),
            ("within_domain_cap", pa.bool_()),
            ("warc_date", pa.string()),
            ("language", pa.string()),
            ("language_confidence", pa.float32()),
            ("paragraph", pa.string()),
            ("matched_keywords", pa.list_(pa.string())),
            ("semantic_score", pa.float32()),
            ("concept_match", pa.string()),
            ("concept_cluster_id", pa.string()),
            ("narrative_score", pa.int16()),
            ("template_fingerprint", pa.string()),
            ("boilerplate_score", pa.int8()),
            ("boilerplate_features", pa.list_(pa.string())),
        ]
    )
    provenance = pa.schema(
        [
            ("schema_version", pa.int16()),
            ("record_id", pa.string()),
            ("story_id", pa.string()),
            ("content_fingerprint", pa.string()),
            ("crawl_id", pa.string()),
            ("source_file", pa.string()),
            ("url", pa.string()),
            ("domain", pa.string()),
            ("warc_date", pa.string()),
            ("language", pa.string()),
            ("language_confidence", pa.float32()),
            ("duplicate_kind", pa.string()),
            ("duplicate_distance", pa.int16()),
            ("is_canonical", pa.bool_()),
        ]
    )
    return stories, provenance


def export_parquet(
    output_dir: str | Path = OUTPUT_DIR,
    parquet_dir: str | Path = PARQUET_DIR,
    dedupe: str = "exact",
    near_distance: int = 3,
    batch_size: int = 1_000,
    domain_share_warning: float = DOMAIN_SHARE_WARNING,
    domain_story_cap: int = DOMAIN_STORY_CAP,
) -> dict:
    """Build canonical stories and complete provenance, then atomically publish them."""
    if dedupe not in {"none", "exact", "near"}:
        raise ValueError("dedupe must be none, exact, or near")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if domain_story_cap <= 0:
        raise ValueError("domain_story_cap must be positive")

    target = Path(parquet_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}-staging-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    backup = target.parent / f".{target.name}-backup-{uuid.uuid4().hex}"
    story_schema, provenance_schema = _schemas()
    story_writer = _PartitionedWriter(
        staging / "stories",
        story_schema,
        ("crawl_id", "language"),
        batch_size,
    )
    provenance_writer = _PartitionedWriter(
        staging / "provenance",
        provenance_schema,
        ("crawl_id", "language"),
        batch_size,
    )
    duplicates = {"exact": 0, "near": 0}
    duplicate_path = staging / "_duplicates.jsonl"
    diversity = DiversityTracker(domain_share_warning)
    capture_domains = Counter()
    canonical_domains = Counter()
    stories_within_domain_cap = 0
    input_rows = 0

    try:
        with DedupIndex(staging / "_dedupe.sqlite", near_distance=near_distance) as index:
            with duplicate_path.open("w", encoding="utf-8") as duplicate_handle:
                for source_record in iter_output_records(output_dir):
                    input_rows += 1
                    record = _record_with_identity(source_record)
                    domain = domain_from_url(record["url"])
                    capture_domains[domain] += 1

                    duplicate = None
                    if dedupe == "none":
                        story_id = record["record_id"]
                    else:
                        story_id = record["story_fingerprint"]
                        duplicate = index.check_and_add(
                            story_id,
                            record["story_fingerprint"],
                            record["paragraph"],
                            dedupe,
                        )
                        if duplicate:
                            story_id = duplicate.canonical_record_id
                            duplicates[duplicate.kind] += 1
                            duplicate_handle.write(
                                json.dumps(
                                    {
                                        "record_id": record["record_id"],
                                        "story_id": story_id,
                                        "kind": duplicate.kind,
                                        "distance": duplicate.distance,
                                    }
                                )
                                + "\n"
                            )

                    is_canonical = duplicate is None
                    provenance_writer.add(
                        {
                            "schema_version": record["schema_version"],
                            "record_id": record["record_id"],
                            "story_id": story_id,
                            "content_fingerprint": record["content_fingerprint"],
                            "crawl_id": record["crawl_id"],
                            "source_file": record["source_file"],
                            "url": record["url"],
                            "domain": domain,
                            "warc_date": record["warc_date"],
                            "language": record["language"],
                            "language_confidence": record["language_confidence"],
                            "duplicate_kind": duplicate.kind if duplicate else "canonical",
                            "duplicate_distance": duplicate.distance if duplicate else 0,
                            "is_canonical": is_canonical,
                        }
                    )

                    if is_canonical:
                        canonical_domains[domain] += 1
                        domain_story_rank = canonical_domains[domain]
                        within_domain_cap = domain_story_rank <= domain_story_cap
                        if within_domain_cap:
                            stories_within_domain_cap += 1
                        features = boilerplate_features(record["paragraph"])
                        story = {
                            **record,
                            "story_id": story_id,
                            "domain": domain,
                            "domain_story_rank": domain_story_rank,
                            "within_domain_cap": within_domain_cap,
                            "concept_cluster_id": concept_cluster_id(
                                record["concept_match"]
                            ),
                            "template_fingerprint": template_fingerprint(
                                record["paragraph"]
                            ),
                            "boilerplate_score": boilerplate_score(features),
                            "boilerplate_features": features,
                        }
                        story_writer.add(story)
                        diversity.observe(story)

                    while (
                        story_writer.buffered_rows + provenance_writer.buffered_rows
                        >= batch_size * 10
                    ):
                        if story_writer.buffered_rows >= provenance_writer.buffered_rows:
                            story_writer.flush_largest()
                        else:
                            provenance_writer.flush_largest()

        story_writer.flush_all()
        provenance_writer.flush_all()
        (staging / "_dedupe.sqlite").unlink(missing_ok=True)

        parquet_files = sorted(staging.rglob("*.parquet"))
        files = []
        for path in parquet_files:
            relative = path.relative_to(staging).as_posix()
            files.append(
                {
                    "table": relative.split("/", 1)[0],
                    "path": relative,
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
        quality_report = diversity.report()
        quality_report.update(
            {
                "domain_story_cap": domain_story_cap,
                "stories_within_domain_cap": stories_within_domain_cap,
                "stories_over_domain_cap": story_writer.rows - stories_within_domain_cap,
            }
        )
        manifest = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "dataset_schema_version": 2,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dedupe_mode": dedupe,
            "near_distance": near_distance if dedupe == "near" else None,
            "input_captures": input_rows,
            "rows": story_writer.rows,
            "duplicates": duplicates,
            "tables": {
                "stories": {
                    "rows": story_writer.rows,
                    "partitions": dict(sorted(story_writer.partition_rows.items())),
                },
                "provenance": {
                    "rows": provenance_writer.rows,
                    "partitions": dict(sorted(provenance_writer.partition_rows.items())),
                },
            },
            "quality": quality_report,
            "capture_domains": [
                {"domain": domain, "captures": count}
                for domain, count in capture_domains.most_common(20)
            ],
            "duplicates_file": {
                "path": duplicate_path.name,
                "bytes": duplicate_path.stat().st_size,
                "sha256": _sha256(duplicate_path),
            },
            "files": files,
        }
        if provenance_writer.rows != input_rows:
            raise RuntimeError("provenance export did not preserve every input capture")
        if story_writer.rows + sum(duplicates.values()) != input_rows:
            raise RuntimeError("canonical and duplicate counts do not reconcile")
        (staging / "_manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )

        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staging, target)
        except Exception:
            if backup.exists():
                os.replace(backup, target)
            raise
        else:
            shutil.rmtree(backup, ignore_errors=True)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
