from story_reconstruction import assemble_story_passages, extract_story_metadata


def _record(index, text):
    return {
        "record_id": f"record-{index}",
        "story_id": f"story-{index}",
        "crawl_id": "crawl",
        "source_file": "source.wet.gz",
        "url": "https://example.test/memory",
        "domain": "example.test",
        "warc_date": "2026-01-01",
        "language": "en",
        "language_confidence": 0.99,
        "document_id": "document",
        "paragraph_index": index,
        "paragraph": text,
        "matched_keywords": ["hometown"],
        "semantic_score": 0.8,
        "narrative_score": 12,
        "within_domain_cap": True,
    }


def test_adjacent_paragraphs_form_passage_with_place_and_time_metadata():
    rows = [
        _record(
            2,
            "I grew up in Toronto during my childhood, surrounded by my family.",
        ),
        _record(
            3,
            "In 1998 we moved from Toronto to Boston and began a new life.",
        ),
    ]
    passages = assemble_story_passages(rows)
    assert len(passages) == 1
    passage = passages[0]
    assert passage["paragraph_count"] == 2
    assert passage["start_paragraph_index"] == 2
    assert passage["end_paragraph_index"] == 3
    assert 1998 in passage["year_mentions"]
    assert "Toronto" in passage["place_mentions"]
    assert "Toronto -> Boston" in passage["migration_routes"]


def test_nonadjacent_paragraphs_remain_separate_passages():
    passages = assemble_story_passages(
        [_record(1, "My home story."), _record(3, "A later memory.")]
    )
    assert len(passages) == 2
    assert extract_story_metadata("No explicit place or date.")["metadata_confidence"] == 0
