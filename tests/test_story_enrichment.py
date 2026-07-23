import gzip
import json

from matcher import Match
from output import OutputWriter
from story_context import expand_story_window
from story_enrichment import enrich_story_sources, export_stories, plan_story_enrichment


def _write_match(writer, source, capture_date):
    paragraphs = [
        "My family supported me through a difficult loss. Their strength carried me.",
        "I could never forget where I came from. I remained proud of my heritage.",
        "Later I wrote to an old friend. I explained why those memories mattered.",
    ]
    story = expand_story_window(paragraphs, 1).payload
    match = Match(
        url="https://example.test/memory",
        warc_date=capture_date,
        text=paragraphs[1],
        matched_keywords=["heritage"],
        semantic_score=0.8,
        concept_match="family heritage",
        crawl_id="CC-MAIN-2026-12",
        narrative_score=12,
        document_id="document",
        paragraph_index=1,
        story=story,
    )
    transaction = writer.begin_source(source)
    transaction.write_matches([match], [("en", 0.99)])
    transaction.commit()


def test_embedded_story_enrichment_is_resumable_and_exports_deduplicated_stories(
    tmp_path,
):
    output_dir = tmp_path / "output"
    stories_dir = tmp_path / "stories"
    export_dir = tmp_path / "exports"
    writer = OutputWriter(output_dir)
    _write_match(writer, "crawl-data/one.warc.wet.gz", "2026-01-01")
    _write_match(writer, "crawl-data/two.warc.wet.gz", "2026-02-01")

    before = plan_story_enrichment(output_dir, stories_dir, limit=10)
    result = enrich_story_sources(output_dir, stories_dir, limit=10)
    after = plan_story_enrichment(output_dir, stories_dir, limit=10)
    exported = export_stories(stories_dir, export_dir)

    assert before["pending_sources"] == 2
    assert result["completed_sources"] == 2
    assert result["stories_written"] == 2
    assert all(row["embedded_stories"] == 1 for row in result["sources"])
    assert after["pending_sources"] == 0
    assert exported["unique_stories"] == 1
    assert exported["source_captures"] == 2
    markdown = (export_dir / "stories_en.md").read_text(encoding="utf-8")
    assert "**Seed:**" in markdown
    assert "**Context Before:**" in markdown
    with gzip.open(export_dir / "stories.jsonl.gz", "rt", encoding="utf-8") as handle:
        row = json.loads(next(handle))
    assert row["capture_count"] == 2


def test_outdated_story_fragment_is_pending_and_not_exported(tmp_path):
    output_dir = tmp_path / "output"
    stories_dir = tmp_path / "stories"
    export_dir = tmp_path / "exports"
    writer = OutputWriter(output_dir)
    _write_match(writer, "crawl-data/old.warc.wet.gz", "2026-01-01")
    enrich_story_sources(output_dir, stories_dir, limit=1)
    fragment = next((stories_dir / "_records").glob("*.jsonl.gz"))
    with gzip.open(fragment, "rt", encoding="utf-8") as handle:
        row = json.loads(next(handle))
    row["story"]["expansion_version"] = "seed-window-obsolete"
    with gzip.open(fragment, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")

    plan = plan_story_enrichment(output_dir, stories_dir, limit=1)
    exported = export_stories(stories_dir, export_dir)

    assert plan["pending_sources"] == 1
    assert plan["pending_matches"] == 1
    assert exported["unique_stories"] == 0
