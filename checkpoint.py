"""Verified project checkpoint and handoff compaction."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from config import DB_ARCHIVE_PATH, DB_PATH, OUTPUT_DIR
from database_checkpoint import archive_database
from output import OutputWriter
from progress import ProgressTracker


def verify_output_integrity(output_dir: str | Path = OUTPUT_DIR) -> dict:
    writer = OutputWriter(output_dir)
    manifests = list(writer.iter_manifests())
    failures = []
    covered_shards = set()
    for manifest in manifests:
        covered_shards.update(shard["path"] for shard in manifest.get("shards", []))
        errors = writer.verify_source(str(manifest["source_file"]))
        if errors:
            failures.append(
                {"source_file": manifest["source_file"], "errors": errors}
            )
    all_shards = {
        path.relative_to(writer.output_dir).as_posix()
        for path in writer.output_dir.glob("*/*.jsonl.gz")
    }
    uncovered = sorted(all_shards - covered_shards)
    return {
        "valid": not failures and not uncovered,
        "manifests": len(manifests),
        "shards": len(all_shards),
        "source_failures": failures,
        "uncovered_shards": uncovered,
        "integrity_errors": len(failures) + len(uncovered),
    }


def create_checkpoint(
    output_dir: str | Path = OUTPUT_DIR,
    db_path: str | Path = DB_PATH,
    verify: bool = True,
    compact_manifests: bool = True,
    compact_database: bool = True,
    force_vacuum: bool = False,
    db_archive_path: str | Path | None = None,
) -> dict:
    """Verify durable state and compact metadata for a workstation handoff."""
    writer = OutputWriter(output_dir)
    tracker = ProgressTracker(db_path)
    summary = tracker.get_summary()
    if summary["processing"]:
        raise RuntimeError("cannot checkpoint while files are marked as processing")

    verification_before = verify_output_integrity(output_dir) if verify else None
    if verification_before is not None and not verification_before["valid"]:
        raise RuntimeError(
            f"output verification found {verification_before['integrity_errors']} errors"
        )

    manifest_result = writer.compact_manifest_catalog() if compact_manifests else None
    database_result = (
        tracker.compact(force_vacuum=force_vacuum) if compact_database else None
    )
    project_checkpoint = Path(db_path).resolve() == DB_PATH.resolve()
    archive_target = (
        Path(db_archive_path)
        if db_archive_path is not None
        else DB_ARCHIVE_PATH
        if project_checkpoint
        else Path(str(db_path) + ".gz")
    )
    archive_result = archive_database(db_path, archive_target)
    replay_result = None
    run_history_result = None
    if project_checkpoint:
        from evaluation import compact_replay_reservoir
        from metrics import compact_run_history

        replay_result = compact_replay_reservoir()
        run_history_result = compact_run_history()
    verification_after = verify_output_integrity(output_dir) if verify else None
    if verification_after is not None and not verification_after["valid"]:
        raise RuntimeError(
            "output verification failed after metadata compaction; handoff was stopped"
        )

    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "ready",
        "progress": summary,
        "verification": verification_after or verification_before,
        "manifest_compaction": manifest_result,
        "database_compaction": database_result,
        "database_archive": archive_result,
        "evaluation_replay": replay_result,
        "run_history": run_history_result,
    }
