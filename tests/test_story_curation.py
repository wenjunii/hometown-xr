import gzip
import json

from story_curation import (
    assess_story,
    curate_story_rows,
    inspect_story_quality,
    write_story_curation,
)


def _story(story_id, text, *, semantic=0.8, ready=True, segments=1):
    return {
        "story_id": story_id,
        "record_id": f"record-{story_id}",
        "match_numbers": [1],
        "language": "en",
        "url": "https://example.test/story",
        "seed": {
            "paragraph": text,
            "semantic_score": semantic,
            "narrative_score": 12,
            "matched_keywords": ["hometown"],
        },
        "story": {
            "text": text,
            "character_count": len(text),
            "paragraph_count": 4,
            "sentence_count": 8,
            "segment_count": segments,
            "story_length_ready": ready,
            "source_text_mode": "verbatim_selected_source_paragraphs",
            "paragraphs": [{"role": "seed", "text": text}],
        },
    }


def test_story_assessment_is_explainable_and_preserves_source_text():
    row = _story(
        "one",
        "I remember my hometown. My family returned every summer. " * 8,
        segments=2,
    )

    result = assess_story(row)

    assert result["source_text_preserved"]
    assert not result["generated_text"]
    assert 0 <= result["quality_score"] <= 1
    assert "non_contiguous_source_segments" in result["warnings"]


def test_curation_ranks_quality_and_clusters_exact_duplicates():
    text = "I remember my hometown and the stories my grandmother told. " * 8
    rows = [
        _story("best", text, semantic=0.9),
        _story("duplicate", text, semantic=0.7),
        _story("short", "A short memory.", semantic=0.95, ready=False),
    ]

    result = curate_story_rows(rows)
    by_id = {row["story_id"]: row for row in result["rows"]}

    assert result["stories"] == 3
    assert result["canonical_stories"] == 2
    assert result["exact_duplicates"] == 1
    assert by_id["duplicate"]["curation"]["duplicate"] == {
        "kind": "exact",
        "canonical_story_id": "best",
        "distance": 0,
    }
    assert by_id["best"]["story"]["text"] == text


def test_curation_export_is_deterministic_and_keeps_full_story_rows(tmp_path):
    source = tmp_path / "stories.jsonl.gz"
    row = _story("one", "I remember my hometown and my family. " * 12)
    with gzip.open(source, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    output = tmp_path / "curated.jsonl.gz"
    report = tmp_path / "report.json"

    result = write_story_curation(source, output, report)
    first = output.read_bytes()
    write_story_curation(source, output, report)

    assert output.read_bytes() == first
    assert result["stories"] == 1
    with gzip.open(output, "rt", encoding="utf-8") as handle:
        exported = json.loads(next(handle))
    assert exported["story"]["text"] == row["story"]["text"]
    assert exported["curation"]["generated_text"] is False


def test_distant_paragraphs_do_not_supply_contiguous_context():
    seed = "My grandmother told me about home."
    raw = "My grandmother told me about home.  "
    distant = "I heard the stories many years before I returned. " * 10
    row = _story("gap", distant + "\n\n" + raw)
    row["seed"]["paragraph"] = seed
    row["story"]["paragraphs"] = [
        {"paragraph_index": 0, "role": "referenced_event", "text": distant},
        {"paragraph_index": 35, "role": "seed", "text": raw},
    ]
    before = json.dumps(row, sort_keys=True)

    quality = inspect_story_quality(row)

    assert quality["largest_gap_paragraphs"] == 34
    assert quality["contiguous_paragraphs"] == 1
    assert {"large_gap", "insufficient_context"} <= quality["issues"].keys()
    assert quality["seed_correspondence"] == "normalized"
    assert quality["source_seed_paragraph"] == raw
    assert json.dumps(row, sort_keys=True) == before


def test_contiguous_paragraphs_keep_a_good_source_window_off_the_queue():
    seed = "I always recognized the sound of my grandparents opening their front door."
    context = (
        "We stayed in their upstairs room on Sundays, after cooking dinner together "
        "and hearing about the small village where they grew up. "
        "One afternoon my brother asked why there were marks on the kitchen wall. "
        "I watched them show him how each mark recorded our height after another summer, "
        "then climbed the stairs to bring back the old photographs that went with each date."
    )
    row = _story("continuous", seed + "\n\n" + context)
    row["seed"]["paragraph"] = seed
    row["story"]["paragraphs"] = [
        {"paragraph_index": 5, "role": "seed", "text": seed},
        {"paragraph_index": 6, "role": "context_after", "text": context},
    ]

    quality = inspect_story_quality(row)

    assert quality["seed_correspondence"] == "exact"
    assert quality["contiguous_paragraphs"] == 2
    assert not quality["issues"]


def test_quality_reports_boilerplate_and_unconfirmed_match_separately():
    raw = "Skip to content. Main menu. Privacy policy. Buy now."
    row = _story("template", raw)
    row["seed"]["paragraph"] = "I went home last autumn."
    quality = inspect_story_quality(row)

    assert quality["seed_correspondence"] == "unconfirmed"
    assert {"seed_unconfirmed", "likely_boilerplate"} <= quality["issues"].keys()
    assert quality["source_seed_paragraph"] == raw
