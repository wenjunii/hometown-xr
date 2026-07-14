"""Versioned, disk-backed cache for reusable model inference artifacts."""

from __future__ import annotations

import logging
import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from config import INFERENCE_CACHE_PATH

_SCHEMA_VERSION = 1
_QUERY_CHUNK = 400
logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunks(values: list[str], size: int = _QUERY_CHUNK):
    for start in range(0, len(values), size):
        yield values[start : start + size]


class InferenceCache:
    """Cache embeddings, semantic scores, and raw language predictions."""

    def __init__(self, path: str | Path = INFERENCE_CACHE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self._init_schema()
        self._closed = False
        self.disabled = False

    def _init_schema(self) -> None:
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, _SCHEMA_VERSION}:
            raise RuntimeError(
                f"Unsupported inference cache schema {version}; remove {self.path} to rebuild it"
            )
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS embeddings (
                namespace TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                dtype TEXT NOT NULL,
                vector BLOB NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (namespace, text_hash)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS semantic_scores (
                namespace TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                score REAL NOT NULL,
                concept TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (namespace, text_hash)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS language_predictions (
                namespace TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                language TEXT NOT NULL,
                confidence REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (namespace, text_hash)
            ) WITHOUT ROWID;
            """
        )
        self.conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        self.conn.commit()

    def __enter__(self) -> "InferenceCache":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _lookup(self, table: str, columns: str, namespace: str, hashes: Iterable[str]):
        if self.disabled:
            return []
        values = list(dict.fromkeys(hashes))
        rows = []
        try:
            for chunk in _chunks(values):
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    self.conn.execute(
                        f"SELECT text_hash, {columns} FROM {table} "
                        f"WHERE namespace = ? AND text_hash IN ({placeholders})",
                        [namespace, *chunk],
                    ).fetchall()
                )
        except sqlite3.DatabaseError as exc:
            self._disable(exc)
            return []
        return rows

    def _disable(self, exc: Exception) -> None:
        if self.disabled:
            return
        self.disabled = True
        try:
            self.conn.rollback()
        except sqlite3.DatabaseError:
            pass
        logger.warning("Inference cache disabled for this run: %s", exc)

    def get_semantic(
        self,
        namespace: str,
        hashes: Iterable[str],
    ) -> dict[str, tuple[float, str]]:
        return {
            str(text_hash): (float(score), str(concept))
            for text_hash, score, concept in self._lookup(
                "semantic_scores",
                "score, concept",
                namespace,
                hashes,
            )
        }

    def put_semantic(
        self,
        namespace: str,
        values: dict[str, tuple[float, str]],
    ) -> None:
        if not values or self.disabled:
            return
        now = _utc_now()
        try:
            self.conn.executemany(
                """
                INSERT INTO semantic_scores(namespace, text_hash, score, concept, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(namespace, text_hash) DO UPDATE SET
                    score = excluded.score,
                    concept = excluded.concept,
                    updated_at = excluded.updated_at
                """,
                [
                    (namespace, text_hash, float(score), str(concept), now)
                    for text_hash, (score, concept) in values.items()
                ],
            )
            self.conn.commit()
        except sqlite3.DatabaseError as exc:
            self._disable(exc)

    def get_embeddings(self, namespace: str, hashes: Iterable[str]):
        import numpy as np

        result = {}
        invalid = []
        for text_hash, dimensions, dtype, vector in self._lookup(
            "embeddings",
            "dimensions, dtype, vector",
            namespace,
            hashes,
        ):
            try:
                raw = zlib.decompress(vector)
                array = np.frombuffer(raw, dtype=np.dtype(str(dtype))).copy()
            except (TypeError, ValueError, zlib.error):
                invalid.append(str(text_hash))
                continue
            if len(array) != int(dimensions):
                invalid.append(str(text_hash))
                continue
            result[str(text_hash)] = array
        if invalid and not self.disabled:
            try:
                self.conn.executemany(
                    "DELETE FROM embeddings WHERE namespace = ? AND text_hash = ?",
                    [(namespace, text_hash) for text_hash in invalid],
                )
                self.conn.commit()
            except sqlite3.DatabaseError as exc:
                self._disable(exc)
        return result

    def put_embeddings(self, namespace: str, values: dict[str, object]) -> None:
        if not values or self.disabled:
            return
        import numpy as np

        now = _utc_now()
        rows = []
        for text_hash, value in values.items():
            array = np.asarray(value)
            dtype = np.float16 if array.dtype == np.float16 else np.float32
            array = np.ascontiguousarray(array, dtype=dtype).reshape(-1)
            rows.append(
                (
                    namespace,
                    text_hash,
                    int(array.size),
                    str(array.dtype),
                    sqlite3.Binary(zlib.compress(array.tobytes(), level=1)),
                    now,
                )
            )
        try:
            self.conn.executemany(
                """
                INSERT INTO embeddings(
                    namespace, text_hash, dimensions, dtype, vector, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, text_hash) DO UPDATE SET
                    dimensions = excluded.dimensions,
                    dtype = excluded.dtype,
                    vector = excluded.vector,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
            self.conn.commit()
        except sqlite3.DatabaseError as exc:
            self._disable(exc)

    def get_languages(
        self,
        namespace: str,
        hashes: Iterable[str],
    ) -> dict[str, tuple[str, float]]:
        return {
            str(text_hash): (str(language), float(confidence))
            for text_hash, language, confidence in self._lookup(
                "language_predictions",
                "language, confidence",
                namespace,
                hashes,
            )
        }

    def put_languages(
        self,
        namespace: str,
        values: dict[str, tuple[str, float]],
    ) -> None:
        if not values or self.disabled:
            return
        now = _utc_now()
        try:
            self.conn.executemany(
                """
                INSERT INTO language_predictions(
                    namespace, text_hash, language, confidence, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(namespace, text_hash) DO UPDATE SET
                    language = excluded.language,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
                """,
                [
                    (namespace, text_hash, language, float(confidence), now)
                    for text_hash, (language, confidence) in values.items()
                ],
            )
            self.conn.commit()
        except sqlite3.DatabaseError as exc:
            self._disable(exc)

    def stats(self) -> dict:
        counts = {
            table: int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("embeddings", "semantic_scores", "language_predictions")
        }
        bytes_on_disk = sum(
            candidate.stat().st_size
            for candidate in (
                self.path,
                Path(str(self.path) + "-wal"),
                Path(str(self.path) + "-shm"),
            )
            if candidate.exists()
        )
        return {
            "schema_version": _SCHEMA_VERSION,
            "disabled": self.disabled,
            "path": str(self.path),
            "bytes": bytes_on_disk,
            **counts,
        }

    def clear(self) -> None:
        self.conn.executescript(
            """
            DELETE FROM embeddings;
            DELETE FROM semantic_scores;
            DELETE FROM language_predictions;
            """
        )
        self.conn.commit()
        self.conn.execute("VACUUM")

    def close(self) -> None:
        if self._closed:
            return
        if not self.disabled:
            self.conn.commit()
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.close()
        self._closed = True
