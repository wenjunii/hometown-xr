import gzip
import json
import sqlite3

import pytest

from matcher import Match
from output import OutputWriter
from progress import ProgressTracker
from refilter_output import _recover_interrupted_swap, refilter

POSITIVE = (
    "I remember my childhood home clearly. My mother and I took the train back "
    "every summer, and we stayed with my family where I grew up."
)
NEGATIVE = (
    "Privacy policy and terms of service. I can create account access with my "
    "password and sign up before reviewing all rights reserved notices."
)


def _match(text):
    return Match(
        url="https://example.test",
        warc_date="2026-01-01",
        text=text,
        matched_keywords=["home"],
        semantic_score=0.9,
        concept_match="home",
        crawl_id="crawl",
    )


def _db_match_count(db_path, source):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT matches_found FROM processing_state WHERE file_path = ?",
            (source,),
        ).fetchone()[0]
    finally:
        conn.close()


def test_refilter_swaps_output_and_database_counts_together(tmp_path):
    db_path = tmp_path / "progress.db"
    output_dir = tmp_path / "output"
    source = "crawl-data/source.warc.wet.gz"
    tracker = ProgressTracker(db_path)
    tracker.initialize_paths([source], "crawl")
    tracker.mark_completed(source, 10, 2)

    transaction = OutputWriter(output_dir).begin_source(source)
    transaction.write_matches([_match(POSITIVE), _match(NEGATIVE)], [("en", 0.9)] * 2)
    transaction.commit()

    kept, removed = refilter(output_dir=output_dir, db_path=db_path)
    assert (kept, removed) == (1, 1)
    assert _db_match_count(db_path, source) == 1
    output_file = next(output_dir.glob("*/*.jsonl.gz"))
    with gzip.open(output_file, "rt", encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle]
    assert [record["paragraph"] for record in records] == [POSITIVE]


def test_refilter_validation_failure_leaves_output_and_db_untouched(tmp_path):
    db_path = tmp_path / "progress.db"
    output_dir = tmp_path / "output"
    source = "crawl-data/source.warc.wet.gz"
    tracker = ProgressTracker(db_path)
    tracker.initialize_paths([source], "crawl")
    tracker.mark_completed(source, 10, 1)

    writer = OutputWriter(output_dir)
    path = writer.output_path("en", source)
    path.parent.mkdir(parents=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("not-json\n")
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="Invalid JSON"):
        refilter(output_dir=output_dir, db_path=db_path)

    assert path.read_bytes() == before
    assert _db_match_count(db_path, source) == 1


def test_refilter_journal_finishes_swapped_database_counts(tmp_path):
    db_path = tmp_path / "progress.db"
    source = "crawl-data/source.warc.wet.gz"
    tracker = ProgressTracker(db_path)
    tracker.initialize_paths([source], "crawl")
    tracker.mark_completed(source, 10, 1)

    output_dir = tmp_path / "output"
    backup_dir = tmp_path / ".refilter-backup"
    staging_dir = tmp_path / ".refilter-staging"
    output_dir.mkdir()
    backup_dir.mkdir()
    (output_dir / "new-marker").write_text("new", encoding="utf-8")
    (backup_dir / "old-marker").write_text("old", encoding="utf-8")
    journal = tmp_path / ".refilter-journal.json"
    journal.write_text(
        json.dumps(
            {
                "state": "swapped",
                "output": str(output_dir),
                "staging": str(staging_dir),
                "backup": str(backup_dir),
                "counts": {source: 3},
            }
        ),
        encoding="utf-8",
    )

    _recover_interrupted_swap(journal, db_path)
    assert _db_match_count(db_path, source) == 3
    assert (output_dir / "new-marker").exists()
    assert not backup_dir.exists()
    assert not journal.exists()
