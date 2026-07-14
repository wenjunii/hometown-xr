"""Compressed, validated SQLite checkpoint transport for workstation handoff."""

from __future__ import annotations

import gzip
import hashlib
import os
import sqlite3
from pathlib import Path

from config import DB_ARCHIVE_PATH, DB_PATH


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gzip_content_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with gzip.open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def database_sync_status(
    db_path: str | Path = DB_PATH,
    archive_path: str | Path = DB_ARCHIVE_PATH,
) -> dict:
    """Report whether local working state exactly matches the shared archive."""
    database = Path(db_path)
    archive = Path(archive_path)
    database_exists = database.exists()
    archive_exists = archive.exists()
    database_digest = _sha256(database) if database_exists else None
    archive_database_digest = _gzip_content_sha256(archive) if archive_exists else None
    return {
        "synchronized": (
            database_exists
            and archive_exists
            and database_digest == archive_database_digest
        ),
        "database_exists": database_exists,
        "archive_exists": archive_exists,
        "database_sha256": database_digest,
        "archive_database_sha256": archive_database_digest,
    }


def archive_database(
    db_path: str | Path = DB_PATH,
    archive_path: str | Path = DB_ARCHIVE_PATH,
) -> dict:
    """Create a deterministic compressed database artifact for Git handoff."""
    source = Path(db_path)
    target = Path(archive_path)
    if not source.exists():
        raise FileNotFoundError(f"progress database is missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with source.open("rb") as input_handle, temporary.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as compressed:
            for chunk in iter(lambda: input_handle.read(1024 * 1024), b""):
                compressed.write(chunk)
    os.replace(temporary, target)
    return {
        "path": str(target),
        "database_bytes": source.stat().st_size,
        "archive_bytes": target.stat().st_size,
        "database_sha256": _sha256(source),
        "archive_sha256": _sha256(target),
    }


def restore_database(
    archive_path: str | Path = DB_ARCHIVE_PATH,
    db_path: str | Path = DB_PATH,
) -> dict:
    """Atomically restore and validate the local SQLite database from the archive."""
    source = Path(archive_path)
    target = Path(db_path)
    if not source.exists():
        raise FileNotFoundError(f"checkpoint archive is missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".restore.tmp")
    try:
        with gzip.open(source, "rb") as compressed, temporary.open("wb") as handle:
            for chunk in iter(lambda: compressed.read(1024 * 1024), b""):
                handle.write(chunk)
        conn = sqlite3.connect(temporary)
        try:
            integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            rows = int(conn.execute("SELECT COUNT(*) FROM processing_state").fetchone()[0])
        finally:
            conn.close()
        if integrity != "ok":
            raise RuntimeError(f"restored database failed quick_check: {integrity}")
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": str(target),
        "rows": rows,
        "bytes": target.stat().st_size,
        "sha256": _sha256(target),
        "source_archive": str(source),
    }
