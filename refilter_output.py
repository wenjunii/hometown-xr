"""Transactionally apply current semantic and narrative rules to output."""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import shutil
import sqlite3
import uuid
from collections import Counter
from pathlib import Path

from config import (
    DB_PATH,
    MIN_NARRATIVE_INDICATORS,
    OUTPUT_DIR,
    SEMANTIC_THRESHOLD,
)
from matcher import NarrativeFilter
from output import _current_filename, _legacy_filename
from run_lock import CrawlerRunLock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger(__name__)


def _write_journal(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _apply_counts(db_path: Path, counts: dict[str, int]) -> None:
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE processing_state SET matches_found = 0 WHERE status = 'completed'")
        for source_path, count in counts.items():
            cursor = conn.execute(
                "UPDATE processing_state SET matches_found = ? "
                "WHERE file_path = ? AND status = 'completed'",
                (count, source_path),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Output source is not a completed database row: {source_path}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _recover_interrupted_swap(journal_path: Path, db_path: Path) -> None:
    if not journal_path.exists():
        return
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    output_dir = Path(journal["output"])
    staging_dir = Path(journal["staging"])
    backup_dir = Path(journal["backup"])
    state = journal["state"]

    if state == "staged":
        shutil.rmtree(staging_dir, ignore_errors=True)
    elif state == "swapping":
        if output_dir.exists() and backup_dir.exists():
            shutil.rmtree(output_dir)
        if backup_dir.exists() and not output_dir.exists():
            os.replace(backup_dir, output_dir)
        shutil.rmtree(staging_dir, ignore_errors=True)
    elif state in ("swapped", "committed"):
        _apply_counts(db_path, journal["counts"])
        shutil.rmtree(backup_dir, ignore_errors=True)
        shutil.rmtree(staging_dir, ignore_errors=True)
    else:
        raise RuntimeError(f"Unknown refilter journal state: {state}")
    journal_path.unlink(missing_ok=True)


class SourceResolver:
    """Resolve legacy shard names and cache source completion states."""

    def __init__(self, db_path: Path, legacy_names: set[str]):
        self.conn = sqlite3.connect(str(db_path))
        self.status_cache: dict[str, str | None] = {}
        self.by_filename: dict[str, str] = {}

        for file_path, status in self.conn.execute(
            "SELECT file_path, status FROM processing_state"
        ):
            candidates = {_legacy_filename(file_path), _current_filename(file_path)}
            for candidate in candidates & legacy_names:
                previous = self.by_filename.get(candidate)
                if previous is not None and previous != file_path:
                    raise RuntimeError(
                        f"Ambiguous legacy output filename {candidate}: "
                        f"{previous!r} and {file_path!r}"
                    )
                self.by_filename[candidate] = file_path
                self.status_cache[file_path] = status

    def close(self) -> None:
        self.conn.close()

    def legacy_source(self, filename: str) -> str:
        try:
            return self.by_filename[filename]
        except KeyError as exc:
            raise RuntimeError(
                f"Cannot map legacy output shard to a database source: {filename}"
            ) from exc

    def status(self, source_path: str) -> str | None:
        if source_path not in self.status_cache:
            row = self.conn.execute(
                "SELECT status FROM processing_state WHERE file_path = ?",
                (source_path,),
            ).fetchone()
            self.status_cache[source_path] = row[0] if row else None
        return self.status_cache[source_path]


def _stage_refiltered_output(
    output_dir: Path,
    staging_dir: Path,
    db_path: Path,
    semantic_threshold: float,
    narrative_threshold: int,
    narrative_filter: NarrativeFilter,
) -> tuple[Counter, int, int]:
    files = sorted(output_dir.glob("*/*.jsonl.gz"))
    resolver = SourceResolver(db_path, {path.name for path in files})
    counts: Counter[str] = Counter()
    kept = 0
    removed = 0

    try:
        for source_file in files:
            relative = source_file.relative_to(output_dir)
            destination = staging_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            file_kept = 0

            with gzip.open(source_file, "rt", encoding="utf-8") as source_handle:
                with gzip.open(destination, "wt", encoding="utf-8") as dest_handle:
                    for line_number, line in enumerate(source_handle, 1):
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise RuntimeError(
                                f"Invalid JSON in {source_file}:{line_number}: {exc}"
                            ) from exc

                        source_path = record.get("source_file")
                        if not source_path:
                            source_path = resolver.legacy_source(source_file.name)
                            record["source_file"] = source_path

                        passes = (
                            resolver.status(source_path) == "completed"
                            and float(record.get("semantic_score", 0)) >= semantic_threshold
                            and narrative_filter.passes(
                                record.get("paragraph", ""), narrative_threshold
                            )
                        )
                        if passes:
                            dest_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                            counts[source_path] += 1
                            file_kept += 1
                            kept += 1
                        else:
                            removed += 1

            if file_kept == 0:
                destination.unlink()
        return counts, kept, removed
    finally:
        resolver.close()


def refilter(
    output_dir: str | Path = OUTPUT_DIR,
    db_path: str | Path = DB_PATH,
    semantic_threshold: float = SEMANTIC_THRESHOLD,
    narrative_threshold: int = MIN_NARRATIVE_INDICATORS,
    dry_run: bool = False,
    narrative_filter: NarrativeFilter | None = None,
) -> tuple[int, int]:
    """Stage, validate, and atomically install filtered output."""
    output_path = Path(output_dir)
    database_path = Path(db_path)
    if not output_path.exists():
        raise FileNotFoundError(f"Output directory does not exist: {output_path}")
    if not database_path.exists():
        raise FileNotFoundError(f"Progress database does not exist: {database_path}")

    parent = output_path.parent
    journal_path = parent / ".refilter-journal.json"
    _recover_interrupted_swap(journal_path, database_path)

    token = uuid.uuid4().hex
    staging_dir = parent / f".refilter-staging-{token}"
    backup_dir = parent / f".refilter-backup-{token}"
    staging_dir.mkdir()
    filter_instance = narrative_filter or NarrativeFilter()

    try:
        counts, kept, removed = _stage_refiltered_output(
            output_path,
            staging_dir,
            database_path,
            semantic_threshold,
            narrative_threshold,
            filter_instance,
        )
        logger.info("Refilter validation complete: %s kept, %s removed", kept, removed)
        if dry_run:
            shutil.rmtree(staging_dir)
            return kept, removed

        journal = {
            "state": "staged",
            "output": str(output_path),
            "staging": str(staging_dir),
            "backup": str(backup_dir),
            "counts": dict(counts),
        }
        _write_journal(journal_path, journal)
        journal["state"] = "swapping"
        _write_journal(journal_path, journal)

        os.replace(output_path, backup_dir)
        os.replace(staging_dir, output_path)
        journal["state"] = "swapped"
        _write_journal(journal_path, journal)

        _apply_counts(database_path, dict(counts))
        journal["state"] = "committed"
        _write_journal(journal_path, journal)

        shutil.rmtree(backup_dir)
        journal_path.unlink()
        return kept, removed
    except Exception:
        if journal_path.exists():
            try:
                _recover_interrupted_swap(journal_path, database_path)
            except Exception:
                logger.exception(
                    "Automatic refilter recovery failed; rerun the command "
                    "to recover from the journal"
                )
        else:
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--semantic-threshold", type=float, default=SEMANTIC_THRESHOLD)
    parser.add_argument("--narrative-threshold", type=int, default=MIN_NARRATIVE_INDICATORS)
    args = parser.parse_args()
    with CrawlerRunLock("refilter"):
        kept, removed = refilter(
            semantic_threshold=args.semantic_threshold,
            narrative_threshold=args.narrative_threshold,
            dry_run=args.dry_run,
        )
    action = "would keep" if args.dry_run else "kept"
    print(f"Refilter {action} {kept} records and removed {removed}.")


if __name__ == "__main__":
    main()
