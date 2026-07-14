import progress
from checkpoint import (
    create_checkpoint,
    verify_output_integrity,
)
from database_checkpoint import archive_database, database_sync_status, restore_database
from matcher import Match
from output import OutputWriter
from progress import ProgressTracker


def test_checkpoint_verifies_and_compacts_manifest_metadata(tmp_path):
    output_dir = tmp_path / "output"
    source = "crawl-data/source.wet.gz"
    writer = OutputWriter(output_dir)
    transaction = writer.begin_source(source)
    transaction.write_matches(
        [
            Match(
                url="https://example.test/story",
                warc_date="2026-01-01",
                text="I remember my childhood home and our family garden.",
                matched_keywords=["home"],
                semantic_score=0.9,
                concept_match="memories of home",
                crawl_id="crawl",
                source_file=source,
                narrative_score=12,
            )
        ],
        [("en", 0.99)],
    )
    transaction.commit()
    db_path = tmp_path / "progress.db"
    ProgressTracker(db_path).initialize_paths([source], "crawl")

    result = create_checkpoint(output_dir, db_path)
    assert result["status"] == "ready"
    assert result["verification"]["valid"]
    assert result["manifest_compaction"]["manifests"] == 1
    assert writer.catalog_path.exists()
    assert list(writer.manifests_dir.glob("*.json")) == []
    assert verify_output_integrity(output_dir)["valid"]
    assert (tmp_path / "progress.db.gz").exists()


def test_compressed_database_checkpoint_round_trips_atomically(tmp_path):
    db_path = tmp_path / "progress.db"
    archive_path = tmp_path / "progress.db.gz"
    tracker = ProgressTracker(db_path)
    tracker.initialize_paths(["one.wet.gz", "two.wet.gz"], "crawl")

    archived = archive_database(db_path, archive_path)
    assert database_sync_status(db_path, archive_path)["synchronized"]
    tracker.initialize_paths(["three.wet.gz"], "crawl")
    assert not database_sync_status(db_path, archive_path)["synchronized"]
    db_path.unlink()
    restored = restore_database(archive_path, db_path)

    assert archived["archive_bytes"] < archived["database_bytes"]
    assert restored["rows"] == 2
    assert ProgressTracker(db_path).get_summary()["total_files"] == 2


def test_project_tracker_auto_restores_missing_database(tmp_path, monkeypatch):
    db_path = tmp_path / "progress.db"
    archive_path = tmp_path / "progress.db.gz"
    tracker = ProgressTracker(db_path)
    tracker.initialize_paths(["one.wet.gz"], "crawl")
    archive_database(db_path, archive_path)
    db_path.unlink()
    monkeypatch.setattr(progress, "DB_PATH", db_path)
    monkeypatch.setattr(progress, "DB_ARCHIVE_PATH", archive_path)

    restored = progress.ProgressTracker(db_path)
    assert restored.get_summary()["total_files"] == 1
