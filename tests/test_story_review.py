import gzip
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from story_review import (
    StoryReviewIndex,
    build_quality_review_report,
    export_reviewed_stories,
    load_story_export,
    load_story_reviews,
)


def _write_export(path):
    rows = [
        {
            "story_id": "story-one",
            "record_id": "record-one",
            "match_numbers": [1],
            "language": "en",
            "url": "https://one.example/story",
            "warc_date": "2026-01-01",
            "capture_count": 1,
            "seed": {
                "paragraph": "I remembered my hometown.",
                "concept_match": "memories of home",
                "matched_keywords": ["hometown"],
                "semantic_score": 0.8,
                "narrative_score": 12,
            },
            "story": {
                "text": "The first exact source story.",
                "character_count": 29,
                "paragraph_count": 1,
                "paragraphs": [
                    {
                        "role": "seed",
                        "text": "The first exact source story.",
                    }
                ],
            },
        },
        {
            "story_id": "story-two",
            "record_id": "record-two",
            "match_numbers": [2],
            "language": "fr",
            "url": "https://two.example/story",
            "warc_date": "2026-01-02",
            "capture_count": 2,
            "seed": {
                "paragraph": "Je me souviens de ma famille.",
                "concept_match": "family memory",
                "matched_keywords": ["famille"],
                "semantic_score": 0.7,
                "narrative_score": 10,
            },
            "story": {
                "text": "The second exact source story.",
                "character_count": 30,
                "paragraph_count": 1,
                "paragraphs": [
                    {
                        "role": "seed",
                        "text": "The second exact source story.",
                    }
                ],
            },
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_story_review_index_filters_saves_and_exports_selected_rows(tmp_path):
    export_path = tmp_path / "stories_complete.jsonl.gz"
    reviews_path = tmp_path / "reviews.jsonl"
    reviewed_path = tmp_path / "stories_reviewed.jsonl.gz"
    _write_export(export_path)
    index = StoryReviewIndex(export_path, reviews_path)

    assert index.status()["stories"] == 2
    assert index.query(search="hometown")["total"] == 1
    assert index.query(language="fr")["stories"][0]["story_id"] == "story-two"
    assert index.query(sort="quality")["stories"][0]["quality_score"] >= 0
    assert index.status()["curation"]["source_text_preserved"]

    index.review(
        "story-one",
        "selected",
        notes="Strong source passage",
        reviewer="tester",
    )
    status = index.status()
    selected = index.query(decision="selected")
    exported = export_reviewed_stories(
        export_path,
        reviews_path,
        reviewed_path,
    )

    assert status["decisions"]["selected"] == 1
    assert selected["stories"][0]["notes"] == "Strong source passage"
    assert exported["selected_stories"] == 1
    with gzip.open(reviewed_path, "rt", encoding="utf-8") as handle:
        row = json.loads(next(handle))
    assert row["story_id"] == "story-one"
    assert row["story"]["text"] == "The first exact source story."
    assert "first exact source story" in reviewed_path.with_suffix("").with_suffix(
        ".md"
    ).read_text(encoding="utf-8")


def test_story_review_rejects_unknown_story_and_supports_unreview(tmp_path):
    export_path = tmp_path / "stories_complete.jsonl.gz"
    reviews_path = tmp_path / "reviews.jsonl"
    _write_export(export_path)
    index = StoryReviewIndex(export_path, reviews_path)

    with pytest.raises(ValueError, match="not present"):
        index.review("missing", "selected")
    index.review("story-one", "unsure")
    index.review("story-one", "unreviewed")

    assert load_story_reviews(reviews_path) == {}


def test_quality_queue_includes_short_excerpts_and_preserves_existing_reviews(tmp_path):
    source = tmp_path / "stories_complete.jsonl.gz"
    short = tmp_path / "stories_short.jsonl.gz"
    reviews = tmp_path / "reviews.jsonl"
    _write_export(source)
    primary = load_story_export(source)
    short_row = {
        **primary[0],
        "story_id": "short-story",
        "match_numbers": [3],
        "seed": {"paragraph": "I remembered home."},
        "story": {
            "story_length_ready": False,
            "paragraphs": [{"paragraph_index": 4, "role": "seed", "text": "I remembered home."}],
        },
    }
    with gzip.open(short, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(short_row) + "\n")
        handle.write(json.dumps(primary[0]) + "\n")
    index = StoryReviewIndex(source, reviews)
    index.review("story-one", "selected")
    index.review("story-two", "unsure")
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}

    result = build_quality_review_report(source, reviews_path=reviews, limit=1)

    assert result["read_only"] and result["contains_source_text"]
    assert result["inputs"]["short_export"] == str(short)
    assert result["stories"] == result["flagged"] == 3
    assert result["queue_total"] == 2
    assert result["queue"][0]["story_id"] == "story-two"
    assert result["queue"][0]["decision"] == "unsure"
    last = build_quality_review_report(source, reviews_path=reviews, offset=1, limit=1)["queue"][0]
    assert last["story_id"] == "short-story"
    assert last["accepted_match_paragraph"] == last["source_seed_paragraph"] == "I remembered home."
    assert {"insufficient_context"} == last["issues"].keys()
    filtered = build_quality_review_report(
        source, reviews_path=reviews, language="en", issue="seed_unconfirmed"
    )
    assert filtered["queue_total"] == 0
    assert filtered["issue_counts"]["insufficient_context"] == 2
    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_quality_report_tolerates_no_short_export_but_not_a_missing_explicit_input(tmp_path):
    source = tmp_path / "stories_complete.jsonl.gz"
    _write_export(source)
    result = build_quality_review_report(source, reviews_path=tmp_path / "missing-reviews.jsonl")
    assert result["inputs"]["short_export"] is None
    assert result["queue_total"] == 2
    with pytest.raises(FileNotFoundError, match="Story export is missing"):
        build_quality_review_report(source, short_export_path=tmp_path / "missing-short.gz")


def test_quality_cli_does_not_access_story_packs(tmp_path, monkeypatch, capsys):
    import story_packing
    from main import main

    def unexpected():
        pytest.fail("quality report must not inspect or restore story packs")

    source = tmp_path / "stories_complete.jsonl.gz"
    _write_export(source)
    monkeypatch.setattr(story_packing, "story_pack_catalog_path", unexpected)
    monkeypatch.setattr(
        sys, "argv", ["main.py", "stories", "quality", "--export", str(source), "--limit", "1"]
    )

    main()

    result = json.loads(capsys.readouterr().out)
    assert result["read_only"] and result["queue_total"] == 2
    assert len(result["queue"]) == 1
    assert list(tmp_path.iterdir()) == [source]


def test_quality_wrapper_reuses_python_without_changing_input(tmp_path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell is not installed")
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "stories.ps1"
    source = tmp_path / "saved excerpts.jsonl.gz"
    reviews = tmp_path / "review decisions.jsonl"
    _write_export(source)
    before = source.read_bytes()

    args = [pwsh, "-NoProfile", "-File", str(script), "-PythonExecutable", sys.executable]
    run = subprocess.run(
        [*args, "-Action", "quality", "-Export", str(source), "-Reviews", str(reviews), "-Limit", "1"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["read_only"] and result["queue_total"] == 2
    assert len(result["queue"]) == 1
    assert source.read_bytes() == before and not reviews.exists()

    rejected = subprocess.run(
        [*args, "-Action", "export"], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert rejected.returncode != 0
    assert "PythonExecutable is valid only with -Action quality" in rejected.stderr
