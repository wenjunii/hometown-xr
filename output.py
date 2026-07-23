"""Crash-safe, idempotent JSONL output grouped by detected language."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from config import OUTPUT_DIR, OUTPUT_SCHEMA_VERSION, SUPPORTED_OUTPUT_SCHEMA_VERSIONS
from record_identity import content_fingerprint, stable_record_id

if TYPE_CHECKING:
    from matcher import Match

logger = logging.getLogger(__name__)

_SAFE_LANGUAGE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
_MANIFEST_CATALOG = "_manifest-catalog.jsonl.gz"


def _source_digest(source_path: str) -> str:
    return hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:16]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_gzip_jsonl(path: Path, rows: list[dict]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _legacy_filename(source_path: str) -> str:
    filename = source_path.replace("/", "_").replace("\\", "_")
    if filename.endswith(".gz"):
        filename = filename[:-3]
    return filename + ".jsonl.gz"


def _current_filename(source_path: str) -> str:
    basename = re.split(r"[/\\]", source_path)[-1]
    if basename.endswith(".gz"):
        basename = basename[:-3]
    basename = re.sub(r"[^A-Za-z0-9._-]+", "_", basename)
    return f"{_source_digest(source_path)}_{basename}.jsonl.gz"


class SourceOutputTransaction:
    """Stage all output for one source and commit it as one logical unit."""

    def __init__(self, writer: "OutputWriter", source_path: str):
        self.writer = writer
        self.source_path = source_path
        self.staging_dir = writer.staging_root / f"{_source_digest(source_path)}-{uuid.uuid4().hex}"
        self.staging_dir.mkdir(parents=True, exist_ok=False)
        self.counts: dict[str, int] = {}
        self._seen_record_ids: set[str] = set()
        self._finished = False

    def __enter__(self) -> "SourceOutputTransaction":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if not self._finished:
            self.abort()

    def write_matches(
        self,
        matches: list[Match],
        languages: list[tuple[str, float]],
    ) -> dict[str, int]:
        """Append one in-memory match batch to source-local staging files."""
        if len(matches) != len(languages):
            raise ValueError("matches and languages must have the same length")

        by_language: dict[str, list[dict]] = {}
        for match, (language, confidence) in zip(matches, languages):
            lang = language if _SAFE_LANGUAGE.fullmatch(language) else "unknown"
            record_id = stable_record_id(
                match.crawl_id,
                self.source_path,
                match.url,
                match.warc_date,
                match.text,
            )
            if record_id in self._seen_record_ids:
                continue
            self._seen_record_ids.add(record_id)
            record = {
                "schema_version": OUTPUT_SCHEMA_VERSION,
                "record_id": record_id,
                "content_fingerprint": content_fingerprint(match.url, match.text),
                "crawl_id": match.crawl_id,
                "source_file": self.source_path,
                "url": match.url,
                "warc_date": match.warc_date,
                "language": lang,
                "language_confidence": round(confidence, 4),
                "paragraph": match.text,
                "matched_keywords": match.matched_keywords,
                "semantic_score": round(match.semantic_score, 4),
                "concept_match": match.concept_match,
                "narrative_score": match.narrative_score,
                "document_id": match.document_id,
                "paragraph_index": match.paragraph_index,
                "context_before": match.context_before,
                "context_after": match.context_after,
                "story": match.story,
                "filter_signature": self.writer.filter_signature,
                "run_id": self.writer.run_id,
            }
            if match.raw_text and match.raw_text != match.text:
                record["raw_paragraph"] = match.raw_text
            by_language.setdefault(lang, []).append(record)

        written: dict[str, int] = {}
        for lang, records in by_language.items():
            stage_path = self.staging_dir / f"{lang}.jsonl.gz"
            with gzip.open(stage_path, "at", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            count = len(records)
            self.counts[lang] = self.counts.get(lang, 0) + count
            written[lang] = count
        return written

    def _manifest(self, stage_paths: list[Path]) -> dict:
        shards = []
        for stage_path in sorted(stage_paths):
            language = stage_path.name[: -len(".jsonl.gz")]
            destination = self.writer.output_path(language, self.source_path)
            shards.append(
                {
                    "language": language,
                    "path": destination.relative_to(self.writer.output_dir).as_posix(),
                    "records": self.counts.get(language, 0),
                    "bytes": stage_path.stat().st_size,
                    "sha256": _sha256(stage_path),
                }
            )
        return {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "source_file": self.source_path,
            "source_digest": _source_digest(self.source_path),
            "records": sum(self.counts.values()),
            "committed_at": datetime.now(timezone.utc).isoformat(),
            "filter_signature": self.writer.filter_signature,
            "run_id": self.writer.run_id,
            "shards": shards,
        }

    def commit(self) -> dict[str, int]:
        """Replace every prior shard and manifest, rolling back on error."""
        if self._finished:
            raise RuntimeError("output transaction is already finished")

        backup_dir = self.staging_dir / "_backup"
        backups: list[tuple[Path, Path]] = []
        installed: list[Path] = []
        stage_paths = list(self.staging_dir.glob("*.jsonl.gz"))
        manifest_stage = None
        if stage_paths:
            manifest_stage = self.staging_dir / "_manifest.json"
            manifest_stage.write_text(
                json.dumps(self._manifest(stage_paths), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        elif self.writer.get_manifest(self.source_path) is not None:
            manifest_stage = self.staging_dir / "_manifest.json"
            tombstone = self._manifest([])
            tombstone["tombstone"] = True
            manifest_stage.write_text(
                json.dumps(tombstone, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        try:
            for existing in self.writer.find_source_artifacts(self.source_path):
                relative = existing.relative_to(self.writer.output_dir)
                backup = backup_dir / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(existing, backup)
                backups.append((backup, existing))

            for stage_path in stage_paths:
                language = stage_path.name[: -len(".jsonl.gz")]
                destination = self.writer.output_path(language, self.source_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage_path, destination)
                installed.append(destination)

            if manifest_stage is not None:
                manifest_destination = self.writer.manifest_path(self.source_path)
                manifest_destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(manifest_stage, manifest_destination)
                installed.append(manifest_destination)
        except Exception:
            for destination in installed:
                destination.unlink(missing_ok=True)
            for backup, original in reversed(backups):
                original.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, original)
            raise
        else:
            result = dict(self.counts)
            self._finished = True
            shutil.rmtree(self.staging_dir, ignore_errors=True)
            return result

    def abort(self) -> None:
        """Discard staged output without touching committed shards."""
        if not self._finished:
            self._finished = True
            shutil.rmtree(self.staging_dir, ignore_errors=True)


class OutputWriter:
    """Create source-scoped output transactions under ``data/output``."""

    def __init__(
        self,
        output_dir: str | Path = OUTPUT_DIR,
        run_id: str = "",
        filter_signature: str = "",
    ):
        self.output_dir = Path(output_dir)
        self.run_id = run_id
        self.filter_signature = filter_signature
        self.staging_root = self.output_dir / ".staging"
        self.manifests_dir = self.output_dir / "_manifests"
        self.catalog_path = self.output_dir / _MANIFEST_CATALOG
        self._catalog_cache: dict[str, dict] | None = None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)

    def output_path(self, language: str, source_path: str) -> Path:
        lang = language if _SAFE_LANGUAGE.fullmatch(language) else "unknown"
        return self.output_dir / lang / _current_filename(source_path)

    def legacy_output_path(self, language: str, source_path: str) -> Path:
        lang = language if _SAFE_LANGUAGE.fullmatch(language) else "unknown"
        return self.output_dir / lang / _legacy_filename(source_path)

    def manifest_path(self, source_path: str) -> Path:
        return self.manifests_dir / f"{_source_digest(source_path)}.json"

    def _load_catalog(self) -> dict[str, dict]:
        if self._catalog_cache is not None:
            return self._catalog_cache
        catalog = {}
        if self.catalog_path.exists():
            with gzip.open(self.catalog_path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    manifest = json.loads(line)
                    catalog[str(manifest["source_file"])] = manifest
        self._catalog_cache = catalog
        return catalog

    def get_manifest(self, source_path: str) -> dict | None:
        """Resolve a loose override first, then the compact catalog."""
        loose_path = self.manifest_path(source_path)
        if loose_path.exists():
            manifest = json.loads(loose_path.read_text(encoding="utf-8"))
        else:
            manifest = self._load_catalog().get(source_path)
        if manifest is None or manifest.get("tombstone"):
            return None
        return manifest

    def iter_manifests(self):
        """Yield the effective manifest set after applying loose overrides."""
        manifests = dict(self._load_catalog())
        for path in sorted(self.manifests_dir.glob("*.json")):
            manifest = json.loads(path.read_text(encoding="utf-8"))
            source_file = str(manifest["source_file"])
            if manifest.get("tombstone"):
                manifests.pop(source_file, None)
            else:
                manifests[source_file] = manifest
        for source_file in sorted(manifests):
            yield manifests[source_file]

    def compact_manifest_catalog(self) -> dict:
        """Merge loose manifests into one deterministic compressed catalog."""
        loose_paths = sorted(self.manifests_dir.glob("*.json"))
        manifests = dict(self._load_catalog())
        tombstones = 0
        for path in loose_paths:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            source_file = str(manifest["source_file"])
            if manifest.get("tombstone"):
                manifests.pop(source_file, None)
                tombstones += 1
            else:
                manifests[source_file] = manifest

        before_bytes = self.catalog_path.stat().st_size if self.catalog_path.exists() else 0
        if manifests:
            _write_gzip_jsonl(
                self.catalog_path,
                [manifests[source_file] for source_file in sorted(manifests)],
            )
        else:
            self.catalog_path.unlink(missing_ok=True)
        for path in loose_paths:
            path.unlink()
        self._catalog_cache = manifests
        return {
            "manifests": len(manifests),
            "loose_manifests_compacted": len(loose_paths),
            "tombstones_applied": tombstones,
            "catalog_bytes_before": before_bytes,
            "catalog_bytes_after": (
                self.catalog_path.stat().st_size if self.catalog_path.exists() else 0
            ),
        }

    def find_source_outputs(self, source_path: str) -> list[Path]:
        """Find both legacy and collision-resistant shards for one source."""
        names = {_legacy_filename(source_path), _current_filename(source_path)}
        paths: list[Path] = []
        for language_dir in self.output_dir.iterdir():
            if not language_dir.is_dir() or language_dir.name.startswith((".", "_")):
                continue
            for name in names:
                candidate = language_dir / name
                if candidate.exists():
                    paths.append(candidate)
        return paths

    def find_source_artifacts(self, source_path: str) -> list[Path]:
        paths = self.find_source_outputs(source_path)
        manifest = self.manifest_path(source_path)
        if manifest.exists():
            paths.append(manifest)
        return paths

    def begin_source(self, source_path: str) -> SourceOutputTransaction:
        return SourceOutputTransaction(self, source_path)

    def cleanup_stale_staging(self, older_than_seconds: int = 86_400) -> int:
        """Remove abandoned staging directories before startup."""
        cutoff = time.time() - older_than_seconds
        removed = 0
        for path in self.staging_root.iterdir():
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
                removed += 1
        return removed

    def verify_source(self, source_path: str) -> list[str]:
        """Return integrity errors for a committed source manifest."""
        manifest = self.get_manifest(source_path)
        if manifest is None:
            return ["manifest is missing"]
        errors = []
        total_records = 0
        if manifest.get("source_file") != source_path:
            errors.append("manifest source does not match requested source")
        for shard in manifest.get("shards", []):
            path = self.output_dir / shard["path"]
            if not path.exists():
                errors.append(f"missing shard: {shard['path']}")
                continue
            if _sha256(path) != shard["sha256"]:
                errors.append(f"checksum mismatch: {shard['path']}")
            row_count = 0
            try:
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, 1):
                        if not line.strip():
                            continue
                        row_count += 1
                        record = json.loads(line)
                        if record.get("source_file") != source_path:
                            errors.append(
                                f"source mismatch: {shard['path']}:{line_number}"
                            )
                        if record.get("schema_version") not in SUPPORTED_OUTPUT_SCHEMA_VERSIONS:
                            errors.append(
                                f"schema mismatch: {shard['path']}:{line_number}"
                            )
                        if not record.get("record_id") or not record.get(
                            "content_fingerprint"
                        ):
                            errors.append(
                                f"missing identity: {shard['path']}:{line_number}"
                            )
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                errors.append(f"invalid shard {shard['path']}: {exc}")
                continue
            total_records += row_count
            if row_count != int(shard.get("records", -1)):
                errors.append(
                    f"row count mismatch: {shard['path']} "
                    f"({row_count} != {shard.get('records')})"
                )
        if total_records != int(manifest.get("records", -1)):
            errors.append(
                f"manifest total mismatch ({total_records} != {manifest.get('records')})"
            )
        return errors

    def write_matches(
        self,
        matches: list[Match],
        languages: list[tuple[str, float]],
        source_path: str,
    ) -> dict[str, int]:
        """Compatibility helper for a complete source batch."""
        transaction = self.begin_source(source_path)
        try:
            transaction.write_matches(matches, languages)
            return transaction.commit()
        except Exception:
            transaction.abort()
            raise
