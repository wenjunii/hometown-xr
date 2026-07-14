"""Crash-safe, idempotent JSONL output grouped by detected language."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from config import OUTPUT_DIR

if TYPE_CHECKING:
    from matcher import Match

logger = logging.getLogger(__name__)

_SAFE_LANGUAGE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


def _source_digest(source_path: str) -> str:
    return hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:16]


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
            record = {
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
            }
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

    def commit(self) -> dict[str, int]:
        """Replace every prior shard for this source, rolling back on error."""
        if self._finished:
            raise RuntimeError("output transaction is already finished")

        backup_dir = self.staging_dir / "_backup"
        backups: list[tuple[Path, Path]] = []
        installed: list[Path] = []

        try:
            for existing in self.writer.find_source_outputs(self.source_path):
                relative = existing.relative_to(self.writer.output_dir)
                backup = backup_dir / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(existing, backup)
                backups.append((backup, existing))

            for stage_path in self.staging_dir.glob("*.jsonl.gz"):
                language = stage_path.name[:-9]
                destination = self.writer.output_path(language, self.source_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage_path, destination)
                installed.append(destination)
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
        """Discard staged output without touching the current committed shards."""
        if not self._finished:
            self._finished = True
            shutil.rmtree(self.staging_dir, ignore_errors=True)


class OutputWriter:
    """Create source-scoped output transactions under ``data/output``."""

    def __init__(self, output_dir: str | Path = OUTPUT_DIR):
        self.output_dir = Path(output_dir)
        self.staging_root = self.output_dir / ".staging"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def output_path(self, language: str, source_path: str) -> Path:
        lang = language if _SAFE_LANGUAGE.fullmatch(language) else "unknown"
        return self.output_dir / lang / _current_filename(source_path)

    def legacy_output_path(self, language: str, source_path: str) -> Path:
        lang = language if _SAFE_LANGUAGE.fullmatch(language) else "unknown"
        return self.output_dir / lang / _legacy_filename(source_path)

    def find_source_outputs(self, source_path: str) -> list[Path]:
        """Find both legacy and collision-resistant shards for one source."""
        names = {_legacy_filename(source_path), _current_filename(source_path)}
        paths: list[Path] = []
        for language_dir in self.output_dir.iterdir():
            if not language_dir.is_dir() or language_dir.name.startswith("."):
                continue
            for name in names:
                candidate = language_dir / name
                if candidate.exists():
                    paths.append(candidate)
        return paths

    def begin_source(self, source_path: str) -> SourceOutputTransaction:
        return SourceOutputTransaction(self, source_path)

    def cleanup_stale_staging(self, older_than_seconds: int = 86_400) -> int:
        """Remove abandoned staging directories before worker startup."""
        cutoff = time.time() - older_than_seconds
        removed = 0
        for path in self.staging_root.iterdir():
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
                removed += 1
        return removed

    def write_matches(
        self,
        matches: list[Match],
        languages: list[tuple[str, float]],
        source_path: str,
    ) -> dict[str, int]:
        """Compatibility helper for callers that have one complete source batch."""
        transaction = self.begin_source(source_path)
        try:
            transaction.write_matches(matches, languages)
            return transaction.commit()
        except Exception:
            transaction.abort()
            raise
