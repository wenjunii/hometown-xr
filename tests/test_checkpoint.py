from checkpoint import create_checkpoint, verify_output_integrity
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
