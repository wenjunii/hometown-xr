import gzip
import json

from matcher import Match
from output import OutputWriter
from story_context import expand_story_window
from story_enrichment import _write_gzip_rows, enrich_story_sources, export_stories
from story_products import (
    build_story_catalog,
    build_story_quality_report,
    story_gap_rows,
    verify_story_integrity,
)


def _write_match(writer, source, capture_date, *, short=False):
    paragraphs = (
        ["I remember my hometown."]
        if short
        else [
            "My family gathered in the kitchen and shared memories of our old home. "
            "Neighbors brought food, relatives opened old albums, and everyone told "
            "stories about the people who had lived on our street before us.",
            "I remained proud of my heritage and never forgot where I came from. "
            "The strength of my family during difficult years showed me why those "
            "memories mattered and why I carried them into my own adult life.",
            "Years later, those stories still shaped my sense of belonging. "
            "I returned each summer, walked the familiar roads, and shared the same "
            "memories with my children so they could understand our family history.",
        ]
    )
    seed_index = 0 if short else 1
    match = Match(
        url="https://example.test/memory",
        warc_date=capture_date,
        text=paragraphs[seed_index],
        matched_keywords=["heritage"],
        semantic_score=0.8,
        concept_match="family heritage",
        crawl_id="CC-MAIN-2026-12",
        narrative_score=12,
        document_id="document",
        paragraph_index=seed_index,
        story=expand_story_window(paragraphs, seed_index).payload,
    )
    transaction = writer.begin_source(source)
    transaction.write_matches([match], [("en", 0.99)])
    transaction.commit()


def test_story_catalog_detects_fragment_tampering(tmp_path):
    output_dir = tmp_path / "output"
    stories_dir = tmp_path / "stories"
    writer = OutputWriter(output_dir)
    _write_match(writer, "crawl-data/one.warc.wet.gz", "2026-01-01")
    enrich_story_sources(output_dir, stories_dir, limit=1)

    catalog = build_story_catalog(stories_dir)
    verified = verify_story_integrity(stories_dir)

    assert catalog["valid"]
    assert catalog["fragments"] == 1
    assert verified["valid"]
    fragment = next((stories_dir / "_records").glob("*.jsonl.gz"))
    fragment.write_bytes(fragment.read_bytes() + b"tampered")
    damaged = verify_story_integrity(stories_dir)
    assert not damaged["valid"]
    assert damaged["fragment_failures"]


def test_story_catalog_rebuild_is_byte_stable(tmp_path):
    output_dir = tmp_path / "output"
    stories_dir = tmp_path / "stories"
    writer = OutputWriter(output_dir)
    _write_match(writer, "crawl-data/stable.warc.wet.gz", "2026-01-01")
    enrich_story_sources(output_dir, stories_dir, limit=1)

    build_story_catalog(stories_dir)
    catalog_path = stories_dir / "_catalog.json.gz"
    first = catalog_path.read_bytes()
    build_story_catalog(stories_dir)

    assert catalog_path.read_bytes() == first


def test_story_catalog_retains_structurally_valid_stale_fragments(tmp_path):
    output_dir = tmp_path / "output"
    stories_dir = tmp_path / "stories"
    writer = OutputWriter(output_dir)
    _write_match(writer, "crawl-data/old.warc.wet.gz", "2026-01-01")
    enrich_story_sources(output_dir, stories_dir, limit=1)
    fragment = next((stories_dir / "_records").glob("*.jsonl.gz"))
    with gzip.open(fragment, "rt", encoding="utf-8") as handle:
        row = json.loads(next(handle))
    row["story"]["expansion_version"] = "older-valid-version"
    _write_gzip_rows(fragment, [row])

    catalog = build_story_catalog(stories_dir)

    assert catalog["valid"]
    assert catalog["current_records"] == 0
    assert catalog["stale_records"] == 1
    assert verify_story_integrity(stories_dir)["valid"]


def test_quality_report_and_gap_rows_cover_missing_sources(tmp_path):
    output_dir = tmp_path / "output"
    stories_dir = tmp_path / "stories"
    writer = OutputWriter(output_dir)
    _write_match(writer, "crawl-data/one.warc.wet.gz", "2026-01-01")
    _write_match(writer, "crawl-data/two.warc.wet.gz", "2026-01-02")
    enrich_story_sources(output_dir, stories_dir, limit=1)

    report = build_story_quality_report(stories_dir, output_dir)
    gaps = story_gap_rows(stories_dir, output_dir)

    assert report["coverage"]["total_sources"] == 2
    assert report["coverage"]["complete_sources"] == 1
    assert report["coverage"]["missing_matches"] == 1
    assert report["verbatim_integrity"]["valid"]
    assert len(gaps) == 1
    assert gaps[0]["status"] == "missing"


def test_export_writes_complete_short_partial_and_provenance_products(tmp_path):
    output_dir = tmp_path / "output"
    stories_dir = tmp_path / "stories"
    export_dir = tmp_path / "exports"
    writer = OutputWriter(output_dir)
    _write_match(writer, "crawl-data/complete.warc.wet.gz", "2026-01-01")
    _write_match(
        writer,
        "crawl-data/short.warc.wet.gz",
        "2026-01-02",
        short=True,
    )
    _write_match(writer, "crawl-data/z-pending.warc.wet.gz", "2026-01-03")
    enrich_story_sources(output_dir, stories_dir, limit=2)

    result = export_stories(stories_dir, export_dir, output_dir=output_dir)

    assert result["tiers"] == {
        "complete": 1,
        "short": 1,
        "partial_or_missing_sources": 1,
        "provenance_rows": 2,
    }
    for filename in (
        "stories_complete.jsonl.gz",
        "stories_short.jsonl.gz",
        "stories_partial.jsonl.gz",
        "stories_provenance.jsonl.gz",
    ):
        with gzip.open(export_dir / filename, "rt", encoding="utf-8") as handle:
            assert json.loads(next(handle))
    quality = json.loads(
        (export_dir / "story_quality_report.json").read_text(encoding="utf-8")
    )
    assert quality["generated_text"] is False
    assert (export_dir / "story_quality_report.md").exists()
