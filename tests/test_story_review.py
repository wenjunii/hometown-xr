import gzip
import json

import pytest

from story_review import (
    StoryReviewIndex,
    export_reviewed_stories,
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
