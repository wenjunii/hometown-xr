"""Disk-backed exact and near-duplicate detection."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from record_identity import hamming_distance, simhash64


@dataclass(frozen=True)
class Duplicate:
    kind: str
    canonical_record_id: str
    distance: int


class DedupIndex:
    """Bound memory use by storing fingerprints and SimHash bands in SQLite."""

    def __init__(self, path: str | Path, near_distance: int = 3):
        if not 0 <= near_distance <= 64:
            raise ValueError("near_distance must be between 0 and 64")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.near_distance = near_distance
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode = OFF")
        self.conn.execute("PRAGMA synchronous = OFF")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                content_fingerprint TEXT PRIMARY KEY,
                record_id TEXT NOT NULL,
                simhash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bands (
                band_index INTEGER NOT NULL,
                band_value INTEGER NOT NULL,
                record_id TEXT NOT NULL,
                simhash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bands
                ON bands(band_index, band_value);
            """
        )

    def __enter__(self) -> "DedupIndex":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.conn.close()

    @staticmethod
    def _bands(value: int):
        for index in range(4):
            yield index, (value >> (index * 16)) & 0xFFFF

    def check_and_add(
        self,
        record_id: str,
        content_fingerprint: str,
        paragraph: str,
        mode: str,
    ) -> Duplicate | None:
        if mode not in {"none", "exact", "near"}:
            raise ValueError("dedupe mode must be none, exact, or near")
        if mode == "none":
            return None

        exact = self.conn.execute(
            "SELECT record_id FROM records WHERE content_fingerprint = ?",
            (content_fingerprint,),
        ).fetchone()
        if exact:
            return Duplicate("exact", str(exact[0]), 0)

        fingerprint = simhash64(paragraph)
        fingerprint_hex = f"{fingerprint:016x}"
        if mode == "near":
            candidates: dict[str, int] = {}
            for band_index, band_value in self._bands(fingerprint):
                rows = self.conn.execute(
                    """
                    SELECT record_id, simhash
                    FROM bands
                    WHERE band_index = ? AND band_value = ?
                    """,
                    (band_index, band_value),
                )
                for candidate_id, candidate_hash in rows:
                    candidates[str(candidate_id)] = int(str(candidate_hash), 16)
            for candidate_id, candidate_hash in candidates.items():
                distance = hamming_distance(fingerprint, candidate_hash)
                if distance <= self.near_distance:
                    return Duplicate("near", candidate_id, distance)

        self.conn.execute(
            "INSERT INTO records(content_fingerprint, record_id, simhash) VALUES (?, ?, ?)",
            (content_fingerprint, record_id, fingerprint_hex),
        )
        self.conn.executemany(
            "INSERT INTO bands(band_index, band_value, record_id, simhash) VALUES (?, ?, ?, ?)",
            [
                (band_index, band_value, record_id, fingerprint_hex)
                for band_index, band_value in self._bands(fingerprint)
            ],
        )
        return None
