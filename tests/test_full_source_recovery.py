import gzip
import json

import full_source_recovery as recovery


def _target(story_id="bounded-story"):
    seed = (
        "I returned to the blue house after twenty years, and my sister was "
        "waiting beside the old kitchen table."
    )
    return {
        "story_id": story_id,
        "record_id": f"record-{story_id}",
        "crawl_id": "CC-MAIN-2013-48",
        "source_file": "crawl-data/CC-MAIN-2013-48/example.warc.wet.gz",
        "url": "https://example.test/home",
        "warc_date": "2013-12-04T13:33:52Z",
        "language": "en",
        "seed": {"paragraph": seed},
        "story": {"text": seed, "story_length_ready": True},
    }


def _record(target=None):
    target = target or _target()
    html = f"""
    <html><head><style>hidden</style><script>ignore()</script></head><body>
      <article>
        <h1>The blue house</h1>
        <p>When I was eight, this street was the whole map of my world.</p>
        <p>{target['seed']['paragraph']}</p>
        <p>We opened the windows, made tea, and told the stories we had kept.</p>
      </article>
    </body></html>
    """.encode()
    return recovery.build_recovery_record(
        [target],
        {
            "url": target["url"],
            "timestamp": "20131204133352",
            "filename": "crawl-data/example.warc.gz",
            "offset": "10",
            "length": "20",
        },
        {"payload": html, "content_type": "text/html; charset=utf-8"},
    )


def test_visible_document_and_narrative_are_deterministic_source_text():
    result = _record()

    assert result["generated_text"] is False
    assert "ignore()" not in result["document"]["text"]
    assert "hidden" not in result["document"]["text"]
    unit = result["narrative_units"][0]
    assert unit["seed_text"] in unit["narrative_text"]
    assert unit["narrative_completeness"]["status"] == "likely_complete"
    assert recovery._valid_recovery(result)


def test_completeness_marks_truncated_and_gated_text():
    truncated = recovery.assess_narrative_completeness(
        "and this is where the surviving page stops",
        boundary_before="document_start",
        boundary_after="document_end",
    )
    gated = recovery.assess_narrative_completeness(
        "Subscribe to continue reading this family story.",
        boundary_before="document_start",
        boundary_after="document_end",
    )

    assert truncated["status"] == "both_missing"
    assert gated["status"] == "gated_or_preview"


def test_recovery_is_resumable_and_portable_export_is_a_checkpoint(tmp_path, monkeypatch):
    target = _target()
    stories_dir = tmp_path / "stories"
    records_dir = tmp_path / "full-sources"
    exports_dir = tmp_path / "full-exports"
    monkeypatch.setattr(recovery, "iter_story_records", lambda unused: iter([target]))

    planned = recovery.plan_full_source_recovery(
        "2013-12",
        stories_dir=stories_dir,
        full_sources_dir=records_dir,
        export_root=exports_dir,
        limit=None,
    )
    run = recovery.recover_full_sources(
        "2013-12",
        stories_dir=stories_dir,
        full_sources_dir=records_dir,
        export_root=exports_dir,
        limit=None,
        workers=1,
        recoverer=lambda targets: _record(targets[0]),
    )
    exported = recovery.export_full_sources(
        "2013-12", full_sources_dir=records_dir, export_root=exports_dir
    )

    assert planned["network_request_ceiling"] == 2
    assert run["completed_captures"] == 1
    assert exported["recovered_story_units"] == 1
    markdown = (exports_dir / "2013-12" / "stories_en.md").read_text("utf-8")
    assert "Full Source Recovery:** `yes`" in markdown
    assert "Narrative Completeness:** `likely_complete`" in markdown
    with gzip.open(
        exports_dir / "2013-12" / "stories_full.jsonl.gz", "rt", encoding="utf-8"
    ) as handle:
        assert json.loads(next(handle))["indexed_capture"]["timestamp"] == "20131204133352"

    # The synchronized structured export remains a resume checkpoint on another PC.
    for path in (records_dir / "_records").glob("*.json.gz"):
        path.unlink()
    resumed = recovery.plan_full_source_recovery(
        "2013-12",
        stories_dir=stories_dir,
        full_sources_dir=records_dir,
        export_root=exports_dir,
        limit=None,
    )
    assert resumed["recovered_captures"] == 1
    assert resumed["pending_captures"] == 0
    assert resumed["network_request_ceiling"] == 0
