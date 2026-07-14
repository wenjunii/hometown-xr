import sqlite3
from datetime import datetime, timezone

import pytest

from progress import ProgressTracker


def _row(db_path, file_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM processing_state WHERE file_path = ?", (file_path,)
        ).fetchone()
    finally:
        conn.close()


def test_migrates_old_checkpoint_and_retries_failed_file(tmp_path):
    db_path = tmp_path / "progress.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE processing_state (
            file_path TEXT PRIMARY KEY,
            crawl_id TEXT,
            status TEXT DEFAULT 'pending',
            records_processed INTEGER DEFAULT 0,
            matches_found INTEGER DEFAULT 0,
            error_message TEXT,
            started_at TEXT,
            completed_at TEXT
        );
        INSERT INTO processing_state (file_path, crawl_id, status)
        VALUES ('failed.wet.gz', 'crawl', 'failed');
        """
    )
    conn.close()

    tracker = ProgressTracker(db_path)
    claim = tracker.claim_files("crawl", 1)[0]
    assert claim.file_path == "failed.wet.gz"
    assert claim.attempt_count == 1
    assert {
        "attempt_count",
        "next_retry_at",
        "lease_id",
        "heartbeat_at",
        "filter_signature",
        "run_id",
    } <= {
        row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(processing_state)")
    }


def test_claim_release_and_lease_guard(tmp_path):
    tracker = ProgressTracker(tmp_path / "progress.db")
    tracker.initialize_paths(["one.wet.gz", "two.wet.gz"], "crawl")
    claim = tracker.claim_files("crawl", 1)[0]

    assert not tracker.mark_completed("one.wet.gz", 10, 1, "wrong-lease")
    assert tracker.release_claim(claim)
    row = _row(tracker.db_path, claim.file_path)
    assert row["status"] == "pending"
    assert row["attempt_count"] == 0


def test_failed_attempt_is_retryable_and_can_be_reset(tmp_path):
    tracker = ProgressTracker(tmp_path / "progress.db")
    tracker.initialize_paths(["one.wet.gz"], "crawl")
    claim = tracker.claim_files("crawl", 1)[0]
    assert tracker.mark_failed(
        claim.file_path, "network error", claim.lease_id, retry_base_seconds=0
    )

    summary = tracker.get_summary("crawl")
    assert summary["failed"] == 1
    assert summary["retryable"] == 1
    retry = tracker.claim_files("crawl", 1)[0]
    assert retry.attempt_count == 2
    assert tracker.release_claim(retry)

    retry = tracker.claim_files("crawl", 1)[0]
    tracker.mark_failed(retry.file_path, "still broken", retry.lease_id, retry_base_seconds=999)
    assert tracker.retry_failed("crawl") == 1
    assert _row(tracker.db_path, "one.wet.gz")["status"] == "pending"


def test_recovers_only_stale_processing_lease(tmp_path):
    tracker = ProgressTracker(tmp_path / "progress.db")
    tracker.initialize_paths(["stale.wet.gz", "live.wet.gz"], "crawl")
    stale, live = tracker.claim_files("crawl", 2)
    conn = sqlite3.connect(tracker.db_path)
    conn.execute(
        "UPDATE processing_state SET heartbeat_at = ? WHERE file_path = ?",
        (datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(), stale.file_path),
    )
    conn.commit()
    conn.close()

    assert tracker.recover_stale_leases(60) == 1
    assert _row(tracker.db_path, stale.file_path)["status"] == "pending"
    assert _row(tracker.db_path, live.file_path)["status"] == "processing"


def test_filter_signatures_report_stamp_and_selectively_reset(tmp_path):
    tracker = ProgressTracker(tmp_path / "progress.db")
    tracker.initialize_paths(["current", "stale", "unknown"], "crawl")
    tracker.mark_completed("current", 10, 1, filter_signature="new", run_id="run")
    tracker.mark_completed("stale", 10, 2, filter_signature="old", run_id="old-run")
    tracker.mark_completed("unknown", 10, 3)

    assert tracker.get_filter_signature_summary("new") == {
        "current_signature": "new",
        "completed": 3,
        "current": 1,
        "unknown": 1,
        "stale": 1,
    }
    assert tracker.reset_stale_completed("new") == 1
    assert _row(tracker.db_path, "stale")["status"] == "pending"
    assert _row(tracker.db_path, "unknown")["status"] == "completed"
    assert tracker.stamp_unknown_completed("new") == 1
    assert _row(tracker.db_path, "unknown")["filter_signature"] == "new"


def test_compact_rebuilds_oversized_without_rowid_schema_and_guards_active_work(tmp_path):
    db_path = tmp_path / "progress.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE processing_state ("
        "file_path TEXT PRIMARY KEY, crawl_id TEXT, status TEXT NOT NULL DEFAULT 'pending', "
        "records_processed INTEGER NOT NULL DEFAULT 0, "
        "matches_found INTEGER NOT NULL DEFAULT 0, error_message TEXT, started_at TEXT, "
        "completed_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, next_retry_at TEXT, "
        "lease_id TEXT, heartbeat_at TEXT) WITHOUT ROWID"
    )
    conn.execute(
        "INSERT INTO processing_state(file_path, crawl_id) VALUES ('one.wet.gz', 'crawl')"
    )
    conn.commit()
    conn.close()

    tracker = ProgressTracker(db_path)
    claim = tracker.claim_files("crawl", 1)[0]
    with pytest.raises(RuntimeError, match="processing"):
        tracker.compact()
    assert tracker.release_claim(claim)

    result = tracker.compact()
    assert result["schema_rebuilt_to_rowid"]
    assert result["rows"] == 1
    schema = sqlite3.connect(db_path).execute(
        "SELECT sql FROM sqlite_master WHERE name = 'processing_state'"
    ).fetchone()[0]
    assert "WITHOUT ROWID" not in schema.upper()
