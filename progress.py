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
    DB_ARCHIVE_PATH,
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
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if (
            path.resolve() == DB_PATH.resolve()
            and not path.exists()
            and DB_ARCHIVE_PATH.exists()
        ):
            from database_checkpoint import restore_database

            logger.info("Restoring missing project database from %s", DB_ARCHIVE_PATH)
            restore_database(DB_ARCHIVE_PATH, path)
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
                    heartbeat_at TEXT,
                    filter_signature TEXT,
                    run_id TEXT
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
                "filter_signature": "TEXT",
                "run_id": "TEXT",
            }
            for name, declaration in migrations.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE processing_state ADD COLUMN {name} {declaration}")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_retry_ready "
                "ON processing_state(crawl_id, status, next_retry_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_filter_signature "
                "ON processing_state(status, filter_signature)"
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
        filter_signature: str = "",
        run_id: str = "",
    ) -> bool:
        """Commit successful source statistics if the caller owns the lease."""
        now = _utc_now().isoformat()
        where = "file_path = ?"
        params: list[object] = [
            records_processed,
            matches_found,
            now,
            filter_signature or None,
            run_id or None,
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
                    filter_signature = ?,
                    run_id = ?,
                    error_message = NULL,
                    next_retry_at = NULL,
                    lease_id = NULL,
                    heartbeat_at = NULL
                WHERE {where}
                """,
                params,
            )
            return cursor.rowcount == 1

    def get_filter_signature_summary(self, current_signature: str) -> dict[str, int | str]:
        """Classify completed work without changing any checkpoint state."""
        with self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status = 'completed' AND filter_signature = ?
                             THEN 1 ELSE 0 END) AS current,
                    SUM(CASE WHEN status = 'completed'
                                  AND COALESCE(filter_signature, '') = ''
                             THEN 1 ELSE 0 END) AS unknown,
                    SUM(CASE WHEN status = 'completed'
                                  AND COALESCE(filter_signature, '') != ''
                                  AND filter_signature != ?
                             THEN 1 ELSE 0 END) AS stale
                FROM processing_state
                """,
                (current_signature, current_signature),
            ).fetchone()
        return {
            "current_signature": current_signature,
            "completed": int(row["completed"] or 0),
            "current": int(row["current"] or 0),
            "unknown": int(row["unknown"] or 0),
            "stale": int(row["stale"] or 0),
        }

    def stamp_unknown_completed(self, current_signature: str) -> int:
        """Adopt audited legacy completions without forcing an expensive recrawl."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                UPDATE processing_state
                SET filter_signature = ?,
                    run_id = COALESCE(run_id, 'historical-audit')
                WHERE status = 'completed'
                  AND COALESCE(filter_signature, '') = ''
                """,
                (current_signature,),
            )
            return cursor.rowcount

    def reset_stale_completed(
        self,
        current_signature: str,
        include_unknown: bool = False,
        crawl_id: str | None = None,
        limit: int | None = None,
    ) -> int:
        """Return stale completed sources to pending while preserving output until replacement."""
        signature_clause = "COALESCE(filter_signature, '') != ?"
        if not include_unknown:
            signature_clause = (
                "COALESCE(filter_signature, '') != '' AND filter_signature != ?"
            )
        clauses = ["status = 'completed'", signature_clause]
        params: list[object] = [current_signature]
        if crawl_id is not None:
            clauses.append("crawl_id = ?")
            params.append(crawl_id)
        query = (
            "SELECT file_path FROM processing_state WHERE "
            + " AND ".join(clauses)
            + " ORDER BY completed_at, file_path"
        )
        if limit is not None:
            if limit <= 0:
                return 0
            query += " LIMIT ?"
            params.append(limit)
        with self._get_conn() as conn:
            paths = [row[0] for row in conn.execute(query, params).fetchall()]
            if not paths:
                return 0
            conn.executemany(
                """
                UPDATE processing_state
                SET status = 'pending',
                    records_processed = 0,
                    matches_found = 0,
                    error_message = NULL,
                    started_at = NULL,
                    completed_at = NULL,
                    attempt_count = 0,
                    next_retry_at = NULL,
                    lease_id = NULL,
                    heartbeat_at = NULL,
                    filter_signature = NULL,
                    run_id = NULL
                WHERE file_path = ? AND status = 'completed'
                """,
                [(path,) for path in paths],
            )
            return len(paths)

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

    def compact(
        self,
        force_vacuum: bool = False,
        vacuum_threshold: float = 0.05,
    ) -> dict:
        """Optimize checkpoint storage while no crawler owns an active lease."""
        if not 0 <= vacuum_threshold <= 1:
            raise ValueError("vacuum_threshold must be between 0 and 1")
        path = Path(self.db_path)
        before_bytes = path.stat().st_size if path.exists() else 0
        conn = self._get_conn()
        schema_rebuilt = False
        vacuumed = False
        try:
            processing = int(
                conn.execute(
                    "SELECT COUNT(*) FROM processing_state WHERE status = 'processing'"
                ).fetchone()[0]
            )
            if processing:
                raise RuntimeError("cannot compact while files are marked as processing")

            schema_sql = str(
                conn.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'processing_state'"
                ).fetchone()[0]
            )
            if "WITHOUT ROWID" in schema_sql.upper():
                conn.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE processing_state_compact (
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
                        heartbeat_at TEXT,
                        filter_signature TEXT,
                        run_id TEXT
                    );

                    INSERT INTO processing_state_compact (
                        file_path, crawl_id, status, records_processed, matches_found,
                        error_message, started_at, completed_at, attempt_count,
                        next_retry_at, lease_id, heartbeat_at, filter_signature, run_id
                    )
                    SELECT
                        file_path, crawl_id, status, records_processed, matches_found,
                        error_message, started_at, completed_at, attempt_count,
                        next_retry_at, lease_id, heartbeat_at, filter_signature, run_id
                    FROM processing_state;

                    DROP TABLE processing_state;
                    ALTER TABLE processing_state_compact RENAME TO processing_state;
                    CREATE INDEX idx_status ON processing_state(status);
                    CREATE INDEX idx_crawl_status ON processing_state(crawl_id, status);
                    CREATE INDEX idx_retry_ready
                        ON processing_state(crawl_id, status, next_retry_at);
                    CREATE INDEX idx_filter_signature
                        ON processing_state(status, filter_signature);
                    COMMIT;
                    """
                )
                schema_rebuilt = True

            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            free_ratio = free_pages / page_count if page_count else 0.0
            if force_vacuum or schema_rebuilt or free_ratio >= vacuum_threshold:
                conn.execute("VACUUM")
                vacuumed = True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        with self._get_conn() as check:
            after_pages = int(check.execute("PRAGMA page_count").fetchone()[0])
            after_free_pages = int(check.execute("PRAGMA freelist_count").fetchone()[0])
            rows = int(check.execute("SELECT COUNT(*) FROM processing_state").fetchone()[0])
        after_bytes = path.stat().st_size if path.exists() else 0
        return {
            "rows": rows,
            "schema_rebuilt_to_rowid": schema_rebuilt,
            "vacuumed": vacuumed,
            "bytes_before": before_bytes,
            "bytes_after": after_bytes,
            "bytes_saved": max(0, before_bytes - after_bytes),
            "pages_after": after_pages,
            "free_pages_after": after_free_pages,
        }
