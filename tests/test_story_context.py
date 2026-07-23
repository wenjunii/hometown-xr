from story_context import expand_story_window


def test_story_window_keeps_precise_seed_and_role_labeled_context():
    paragraphs = [
        "CHAPTER FOUR",
        "My family had lived in this town for generations. "
        "We knew every street, every shop, and every neighbor who watched us grow.",
        "When my brother died, everyone came together. Their strength carried me.",
        "I could never forget who I was or where I came from. I was proud of my heritage.",
        "On April 18 I wrote to my friend. I explained why I was not ready to speak.",
        "Dear Friend",
        "This letter belongs to a separate structural section.",
    ]

    story = expand_story_window(paragraphs, 3).payload

    assert story["selection_policy"] == (
        "precise_seed_with_unfiltered_document_context"
    )
    assert [row["role"] for row in story["paragraphs"]] == [
        "context_before",
        "context_before",
        "seed",
        "context_after",
    ]
    assert story["start_paragraph_index"] == 1
    assert story["end_paragraph_index"] == 4
    assert story["boundary_before"] == "context_limit"
    assert story["boundary_after"] == "structural_boundary"
    assert story["story_length_ready"]
    assert story["readiness_basis"] == "minimum_characters_and_sentences"
    assert story["sentence_count"] == 8
    assert len(story["story_fingerprint"]) == 64


def test_short_single_sentence_story_is_marked_incomplete():
    story = expand_story_window(["I remember my hometown."], 0).payload

    assert story["paragraph_count"] == 1
    assert story["sentence_count"] == 1
    assert not story["story_length_ready"]
    assert story["boundary_before"] == "document_start"
    assert story["boundary_after"] == "document_end"


def test_story_window_stops_before_a_letter_salutation():
    story = expand_story_window(
        [
            "My family remembered the place where we grew up. We returned every year.",
            "That history made me proud of my heritage. I carried it wherever I went.",
            "Sandra answered my questions and then began a separate letter.",
            "Dear Mary and Bernard:",
            "Thank you for writing to me about an unrelated matter.",
        ],
        1,
    ).payload

    assert story["end_paragraph_index"] == 2
    assert story["boundary_after"] == "structural_boundary"
    assert "Dear Mary and Bernard" not in story["text"]


def test_story_window_removes_an_introduction_to_an_excluded_letter():
    story = expand_story_window(
        [
            "I remained proud of my heritage. I remembered where I came from.",
            "I wrote to Sandra after my brother died. I explained my decision.",
            "Sandra responded with another warm letter:",
            "Dear Mary and Bernard:",
            "Thank you for writing.",
        ],
        0,
    ).payload

    assert story["end_paragraph_index"] == 1
    assert story["boundary_after"] == "structural_boundary"
    assert "Sandra responded" not in story["text"]


def test_story_window_preserves_verbatim_source_paragraphs():
    normalized = [
        "My family lived here for years. We knew every road.",
        "I remember my childhood home and my grandmother's kitchen.",
    ]
    source = [
        "My family lived here for years.\nWe knew every road.",
        "I remember my childhood home &amp;\nand my grandmother's kitchen.",
    ]

    story = expand_story_window(
        normalized,
        1,
        source_paragraphs=source,
    ).payload

    assert story["source_text_mode"] == "verbatim_extracted_paragraphs"
    assert story["text"] == "\n\n".join(source)
    assert story["paragraphs"][1]["text"] == source[1]
    assert story["paragraphs"][1]["normalized_text"] == normalized[1]
    assert len(story["source_text_sha256"]) == 64
    assert all(len(row["source_text_sha256"]) == 64 for row in story["paragraphs"])
