"""SQLite progress tracking with retryable, leased work claims."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from config import (
    DB_PATH,
    LEASE_TIMEOUT_SECONDS,
    MAX_FILE_ATTEMPTS,
    RETRY_BASE_SECONDS,
    RETRY_MAX_SECONDS,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ClaimedFile:
    """A file claim owned by one parent process until completion or release."""

    file_path: str
    lease_id: str
    attempt_count: int


class ProgressTracker:
    """Track files and atomically lease ready work to crawler processes."""

    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_db(self) -> None:
        """Create the current schema and migrate older checkpoints in place."""
        with self._get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS processing_state (
                    file_path TEXT PRIMARY KEY,
                    crawl_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    records_processed INTEGER NOT NULL DEFAULT 0,
                    matches_found INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    lease_id TEXT,
                    heartbeat_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_status
                    ON processing_state(status);
                CREATE INDEX IF NOT EXISTS idx_crawl_status
                    ON processing_state(crawl_id, status);
                """
            )

            columns = {row["name"] for row in conn.execute("PRAGMA table_info(processing_state)")}
            migrations = {
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "next_retry_at": "TEXT",
                "lease_id": "TEXT",
                "heartbeat_at": "TEXT",
            }
            for name, declaration in migrations.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE processing_state ADD COLUMN {name} {declaration}")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_retry_ready "
                "ON processing_state(crawl_id, status, next_retry_at)"
            )

    def initialize_paths(self, file_paths: list[str], crawl_id: str = "") -> None:
        """Add newly discovered source paths without changing existing state."""
        with self._get_conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO processing_state (file_path, crawl_id) VALUES (?, ?)",
                [(path, crawl_id) for path in file_paths],
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM processing_state WHERE crawl_id = ?",
                (crawl_id,),
            ).fetchone()[0]
        logger.info("Progress database has %s tracked files for %s", count, crawl_id)

    def recover_stale_leases(self, max_age_seconds: int = LEASE_TIMEOUT_SECONDS) -> int:
        """Release claims whose parent process stopped sending heartbeats."""
        cutoff = (_utc_now() - timedelta(seconds=max_age_seconds)).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                UPDATE processing_state
                SET status = 'pending',
                    started_at = NULL,
                    heartbeat_at = NULL,
                    lease_id = NULL,
                    attempt_count = CASE
                        WHEN attempt_count > 0 THEN attempt_count - 1 ELSE 0 END
                WHERE status = 'processing'
                  AND (
                    COALESCE(heartbeat_at, started_at) IS NULL
                    OR COALESCE(heartbeat_at, started_at) < ?
                  )
                """,
                (cutoff,),
            )
            recovered = cursor.rowcount
        if recovered:
            logger.info("Recovered %s stale processing leases", recovered)
        return recovered

    def claim_files(
        self,
        crawl_id: str,
        limit: int,
        max_attempts: int = MAX_FILE_ATTEMPTS,
    ) -> list[ClaimedFile]:
        """Atomically claim ready pending or retryable failed files."""
        if limit <= 0:
            return []

        now = _utc_now().isoformat()
        claims: list[ClaimedFile] = []
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT file_path, attempt_count
                FROM processing_state
                WHERE crawl_id = ?
                  AND (
                    status = 'pending'
                    OR (
                      status = 'failed'
                      AND attempt_count < ?
                      AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    )
                  )
                ORDER BY CASE WHEN status = 'failed' THEN 0 ELSE 1 END,
                         completed_at,
                         file_path
                LIMIT ?
                """,
                (crawl_id, max_attempts, now, limit),
            ).fetchall()

            for row in rows:
                lease_id = uuid.uuid4().hex
                attempt_count = int(row["attempt_count"] or 0) + 1
                conn.execute(
                    """
                    UPDATE processing_state
                    SET status = 'processing',
                        attempt_count = ?,
                        lease_id = ?,
                        started_at = ?,
                        heartbeat_at = ?,
                        next_retry_at = NULL
                    WHERE file_path = ?
                    """,
                    (attempt_count, lease_id, now, now, row["file_path"]),
                )
                claims.append(ClaimedFile(row["file_path"], lease_id, attempt_count))
            conn.commit()
            return claims
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def heartbeat_claims(self, claims: Iterable[ClaimedFile]) -> int:
        """Refresh active leases so another run cannot recover live work."""
        claim_list = list(claims)
        if not claim_list:
            return 0
        now = _utc_now().isoformat()
        updated = 0
        with self._get_conn() as conn:
            for claim in claim_list:
                cursor = conn.execute(
                    """
                    UPDATE processing_state SET heartbeat_at = ?
                    WHERE file_path = ? AND status = 'processing' AND lease_id = ?
                    """,
                    (now, claim.file_path, claim.lease_id),
                )
                updated += cursor.rowcount
        return updated

    def release_claim(self, claim: ClaimedFile) -> bool:
        """Return an interrupted or cancelled claim to pending work."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                UPDATE processing_state
                SET status = 'pending',
                    started_at = NULL,
                    heartbeat_at = NULL,
                    lease_id = NULL,
                    next_retry_at = NULL,
                    attempt_count = CASE
                        WHEN attempt_count > 0 THEN attempt_count - 1 ELSE 0 END
                WHERE file_path = ? AND status = 'processing' AND lease_id = ?
                """,
                (claim.file_path, claim.lease_id),
            )
            return cursor.rowcount == 1

    def release_claims(self, claims: Iterable[ClaimedFile]) -> int:
        return sum(1 for claim in claims if self.release_claim(claim))

    def mark_completed(
        self,
        file_path: str,
        records_processed: int,
        matches_found: int,
        lease_id: str | None = None,
    ) -> bool:
        """Commit successful source statistics if the caller owns the lease."""
        now = _utc_now().isoformat()
        where = "file_path = ?"
        params: list[object] = [
            records_processed,
            matches_found,
            now,
            file_path,
        ]
        if lease_id is not None:
            where += " AND status = 'processing' AND lease_id = ?"
            params.append(lease_id)

        with self._get_conn() as conn:
            cursor = conn.execute(
                f"""
                UPDATE processing_state
                SET status = 'completed',
                    records_processed = ?,
                    matches_found = ?,
                    completed_at = ?,
                    error_message = NULL,
                    next_retry_at = NULL,
                    lease_id = NULL,
                    heartbeat_at = NULL
                WHERE {where}
                """,
                params,
            )
            return cursor.rowcount == 1

    def mark_failed(
        self,
        file_path: str,
        error: str,
        lease_id: str | None = None,
        retry_base_seconds: int = RETRY_BASE_SECONDS,
    ) -> bool:
        """Record a failed attempt and schedule exponential-backoff retry."""
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            where = "file_path = ?"
            lookup_params: list[object] = [file_path]
            if lease_id is not None:
                where += " AND status = 'processing' AND lease_id = ?"
                lookup_params.append(lease_id)
            row = conn.execute(
                f"SELECT attempt_count FROM processing_state WHERE {where}",
                lookup_params,
            ).fetchone()
            if row is None:
                conn.rollback()
                return False

            attempt = max(1, int(row["attempt_count"] or 0))
            delay = min(
                RETRY_MAX_SECONDS,
                retry_base_seconds * (2 ** max(0, attempt - 1)),
            )
            now = _utc_now()
            next_retry = (now + timedelta(seconds=delay)).isoformat()
            cursor = conn.execute(
                f"""
                UPDATE processing_state
                SET status = 'failed',
                    error_message = ?,
                    completed_at = ?,
                    next_retry_at = ?,
                    lease_id = NULL,
                    heartbeat_at = NULL
                WHERE {where}
                """,
                [error[:4000], now.isoformat(), next_retry, *lookup_params],
            )
            conn.commit()
            return cursor.rowcount == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def retry_failed(self, crawl_id: str | None = None) -> int:
        """Reset failed files so an operator-requested retry starts immediately."""
        where = "status = 'failed'"
        params: tuple[object, ...] = ()
        if crawl_id is not None:
            where += " AND crawl_id = ?"
            params = (crawl_id,)
        with self._get_conn() as conn:
            cursor = conn.execute(
                f"""
                UPDATE processing_state
                SET status = 'pending',
                    attempt_count = 0,
                    next_retry_at = NULL,
                    started_at = NULL,
                    heartbeat_at = NULL,
                    lease_id = NULL
                WHERE {where}
                """,
                params,
            )
            return cursor.rowcount

    def get_summary(self, crawl_id: str | None = None) -> dict[str, int | float]:
        """Return aggregate state, including work ready for this run."""
        now = _utc_now().isoformat()
        scope = ""
        params: list[object] = [MAX_FILE_ATTEMPTS, now, MAX_FILE_ATTEMPTS]
        if crawl_id is not None:
            scope = "WHERE crawl_id = ?"
            params.append(crawl_id)

        with self._get_conn() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END) AS processing,
                    SUM(CASE WHEN status = 'failed'
                              AND attempt_count < ?
                              AND (next_retry_at IS NULL OR next_retry_at <= ?)
                             THEN 1 ELSE 0 END) AS retryable,
                    SUM(CASE WHEN status = 'failed' AND attempt_count >= ?
                             THEN 1 ELSE 0 END) AS exhausted,
                    COALESCE(SUM(records_processed), 0) AS total_records,
                    COALESCE(SUM(matches_found), 0) AS total_matches
                FROM processing_state
                {scope}
                """,
                params,
            ).fetchone()

        total = int(row["total"] or 0)
        completed = int(row["completed"] or 0)
        pending = int(row["pending"] or 0)
        retryable = int(row["retryable"] or 0)
        return {
            "total_files": total,
            "completed": completed,
            "failed": int(row["failed"] or 0),
            "pending": pending,
            "processing": int(row["processing"] or 0),
            "retryable": retryable,
            "exhausted": int(row["exhausted"] or 0),
            "ready": pending + retryable,
            "total_records": int(row["total_records"] or 0),
            "total_matches": int(row["total_matches"] or 0),
            "progress_pct": (completed / total * 100) if total else 0.0,
        }

    def get_per_crawl_summary(self) -> list[dict[str, int | str]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT crawl_id,
                       COUNT(*) AS total,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END) AS processing,
                       COALESCE(SUM(matches_found), 0) AS matches
                FROM processing_state
                GROUP BY crawl_id
                ORDER BY crawl_id
                """
            ).fetchall()
        return [dict(row) for row in rows]
