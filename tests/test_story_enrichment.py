import gzip
import json
import threading

import pytest

import story_enrichment
from matcher import Match
from output import OutputWriter
from story_context import expand_story_window
from story_enrichment import enrich_story_sources, export_stories, plan_story_enrichment


def _write_match(
    writer,
    source,
    capture_date,
    short=False,
    paragraphs=None,
    source_paragraphs=None,
    concept_match="family heritage",
):
    if paragraphs is None:
        paragraphs = (
            ["I remember my hometown."]
            if short
            else [
            "My family supported me through a difficult loss. "
            "Their strength carried me, and our neighbors brought food every evening. "
            "They sat with us and shared memories of the years we had spent together.",
            "I could never forget where I came from. "
            "I remained proud of my heritage and the stories my grandmother taught me. "
            "Those memories shaped how I understood home and who I had become.",
            "Later I wrote to an old friend. "
            "I explained why those memories mattered and why I still returned each summer. "
            "Walking those familiar streets helped me feel connected to my family again.",
            ]
        )
    seed_index = 0 if short else 1
    if len(paragraphs) == 1:
        seed_index = 0
    story = expand_story_window(
        paragraphs,
        seed_index,
        source_paragraphs=source_paragraphs,
    ).payload
    match = Match(
        url="https://example.test/memory",
        warc_date=capture_date,
        text=paragraphs[seed_index],
        matched_keywords=["heritage"],
        semantic_score=0.8,
        concept_match=concept_match,
        crawl_id="CC-MAIN-2026-12",
        narrative_score=12,
        document_id="document",
        paragraph_index=seed_index,
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
    exported = export_stories(stories_dir, export_dir, output_dir=output_dir)

    assert before["pending_sources"] == 2
    assert result["completed_sources"] == 2
    assert result["stories_written"] == 2
    assert all(row["embedded_stories"] == 1 for row in result["sources"])
    assert after["pending_sources"] == 0
    assert exported["unique_stories"] == 1
    assert exported["source_captures"] == 2
    markdown = (export_dir / "stories_en.md").read_text(encoding="utf-8")
    assert "### Source Story for Matches 1, 2" in markdown
    assert "**Matches Export References:** `matches_en.md` #1, #2" in markdown
    assert "#### Extracted Source Story" in markdown
    assert "**Filter-Matched Paragraph:** 2 of 3" in markdown
    assert "**Seed:**" not in markdown
    with gzip.open(export_dir / "stories.jsonl.gz", "rt", encoding="utf-8") as handle:
        row = json.loads(next(handle))
    assert row["capture_count"] == 2
    assert row["match_numbers"] == [1, 2]
    assert [capture["match_number"] for capture in row["captures"]] == [1, 2]


def test_story_export_excludes_short_passages_by_default(tmp_path):
    output_dir = tmp_path / "output"
    stories_dir = tmp_path / "stories"
    export_dir = tmp_path / "exports"
    writer = OutputWriter(output_dir)
    _write_match(
        writer,
        "crawl-data/short.warc.wet.gz",
        "2026-01-01",
        short=True,
    )
    enrich_story_sources(output_dir, stories_dir, limit=1)

    strict = export_stories(stories_dir, export_dir, output_dir=output_dir)
    inclusive = export_stories(
        stories_dir,
        export_dir,
        include_short=True,
        output_dir=output_dir,
    )

    assert strict["unique_stories"] == 0
    assert strict["excluded_short_passages"] == 1
    assert inclusive["unique_stories"] == 1
    assert inclusive["include_short"]


def test_story_export_keeps_the_accepted_filter_seed_authoritative(tmp_path):
    anchor = (
        "My grandmother used to tell me stories about our ancestors. "
        "Those stories made me proud of where my family comes from."
    )
    matching = [
        "I have many memories of my grandmother. Our family stories about iconic "
        "ancestors and traditions passed through generations became part of my own "
        "history. She recounted her parents and grandparents whenever we visited. "
        "Those tales helped me understand the family roots that shaped us. "
        "I carried those memories into adulthood and shared them with my children."
    ]
    raw_matching = [
        matching[0].replace("grandmother.", "grandmother â€“").replace(
            "family stories", "family &amp; stories"
        )
    ]
    mismatch = [
        "My family supported me during a painful loss. Their strength reminded me "
        "that I could never forget where I came from, and I remained proud of my "
        "heritage. Friends brought food and sat with us each evening. Their care "
        "helped me understand how deeply I belonged to this community and how much "
        "its history had shaped my identity over the years. I continued returning "
        "to that place whenever I needed to feel connected to the people I loved."
    ]
    output_dir = tmp_path / "output"
    stories_dir = tmp_path / "stories"
    export_dir = tmp_path / "exports"
    writer = OutputWriter(output_dir)
    _write_match(
        writer,
        "crawl-data/matching.warc.wet.gz",
        "2026-01-01",
        paragraphs=matching,
        source_paragraphs=raw_matching,
        concept_match=anchor,
    )
    _write_match(
        writer,
        "crawl-data/mismatch.warc.wet.gz",
        "2026-01-02",
        paragraphs=mismatch,
        concept_match=anchor,
    )
    enrich_story_sources(output_dir, stories_dir, limit=2)

    result = export_stories(stories_dir, export_dir, output_dir=output_dir)
    markdown = (export_dir / "stories_en.md").read_text(encoding="utf-8")

    assert result["unique_stories"] == 2
    assert "excluded_anchor_mismatches" not in result
    assert "### Source Story for Match 1" in markdown
    assert "### Source Story for Match 2" in markdown
    assert "**Matches Export References:** `matches_en.md` #1" in markdown
    assert "**Matches Export References:** `matches_en.md` #2" in markdown
    assert "Nearest Semantic Reference (Not a Summary)" in markdown
    assert markdown.count("#### Accepted Filter Paragraph") == 2
    assert "deterministic source-paragraph selection; no generated text" in markdown
    assert "grandmother –" in markdown
    assert "family & stories" in markdown
    assert "painful loss" in markdown


def test_story_enrichment_stops_between_atomic_source_fragments(
    tmp_path,
    monkeypatch,
):
    output_dir = tmp_path / "output"
    stories_dir = tmp_path / "stories"
    writer = OutputWriter(output_dir)
    _write_match(writer, "crawl-data/one.warc.wet.gz", "2026-01-01")
    _write_match(writer, "crawl-data/two.warc.wet.gz", "2026-02-01")
    shutdown_event = threading.Event()
    original_write = story_enrichment._write_gzip_rows
    writes = 0

    def interrupt_after_first_fragment(path, rows):
        nonlocal writes
        original_write(path, rows)
        writes += 1
        if writes == 1:
            shutdown_event.set()

    monkeypatch.setattr(
        story_enrichment,
        "_write_gzip_rows",
        interrupt_after_first_fragment,
    )

    result = enrich_story_sources(
        output_dir,
        stories_dir,
        limit=2,
        workers=1,
        shutdown_event=shutdown_event,
    )
    resumed_plan = plan_story_enrichment(output_dir, stories_dir, limit=2)

    assert result["interrupted"]
    assert result["completed_sources"] == 1
    assert result["interrupted_sources"] == 0
    assert result["remaining_selected_sources"] == 1
    assert resumed_plan["complete_sources"] == 1
    assert resumed_plan["pending_sources"] == 1
    assert not list(stories_dir.rglob("*.tmp"))


def test_story_enrichment_bounds_parallel_source_workers(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    stories_dir = tmp_path / "stories"
    writer = OutputWriter(output_dir)
    for index in range(5):
        _write_match(
            writer,
            f"crawl-data/{index}.warc.wet.gz",
            f"2026-01-{index + 1:02d}",
        )

    original_write = story_enrichment._write_gzip_rows
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    def observed_write(path, rows):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if peak == 3:
                release.set()
        try:
            assert release.wait(timeout=5)
            original_write(path, rows)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(story_enrichment, "_write_gzip_rows", observed_write)

    result = enrich_story_sources(
        output_dir,
        stories_dir,
        limit=5,
        workers=3,
    )

    assert result["workers"] == 3
    assert result["completed_sources"] == 5
    assert result["remaining_selected_sources"] == 0
    assert peak == 3
    assert not list(stories_dir.rglob("*.tmp"))


@pytest.mark.parametrize("workers", [0, 17])
def test_story_enrichment_rejects_unsafe_worker_counts(tmp_path, workers):
    with pytest.raises(ValueError, match="workers must be between 1 and 16"):
        enrich_story_sources(
            tmp_path / "output",
            tmp_path / "stories",
            workers=workers,
        )


def test_story_enrichment_stops_submitting_sources_after_parallel_shutdown(
    tmp_path,
    monkeypatch,
):
    output_dir = tmp_path / "output"
    stories_dir = tmp_path / "stories"
    writer = OutputWriter(output_dir)
    for index in range(5):
        _write_match(
            writer,
            f"crawl-data/{index}.warc.wet.gz",
            f"2026-01-{index + 1:02d}",
        )

    shutdown_event = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    started = []
    original_enrich = story_enrichment._enrich_story_source

    def interrupt_first_wave(source_file, records, target_dir, event):
        with lock:
            started.append(source_file)
            if len(started) == 3:
                shutdown_event.set()
                release.set()
        assert release.wait(timeout=5)
        return original_enrich(source_file, records, target_dir, event)

    monkeypatch.setattr(
        story_enrichment,
        "_enrich_story_source",
        interrupt_first_wave,
    )

    result = enrich_story_sources(
        output_dir,
        stories_dir,
        limit=5,
        workers=3,
        shutdown_event=shutdown_event,
    )
    resumed_plan = plan_story_enrichment(output_dir, stories_dir, limit=5)

    assert result["interrupted"]
    assert result["completed_sources"] == 3
    assert result["remaining_selected_sources"] == 2
    assert len(started) == 3
    assert resumed_plan["complete_sources"] == 3
    assert resumed_plan["pending_sources"] == 2
    assert not list(stories_dir.rglob("*.tmp"))


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
    exported = export_stories(stories_dir, export_dir, output_dir=output_dir)

    assert plan["pending_sources"] == 1
    assert plan["pending_matches"] == 1
    assert exported["unique_stories"] == 0
