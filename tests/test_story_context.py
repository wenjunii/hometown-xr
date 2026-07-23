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


def test_story_window_links_a_referenced_family_loss_without_generation():
    paragraphs = [
        "On April 4, my youngest brother Michael died.",
        "I was the oldest and he the youngest. We were very close.",
        "Michael knew that the family supported him and never stopped loving him.",
        "His death devastated me. I could not believe we would never talk again.",
        "Michael was buried next to our grandfather. Some griefs cannot be overcome.",
        "On April 6, the following newspaper article appeared:",
        "JEWISH LEADERS SEEK EXONERATION",
        "The quoted article discussed a different part of the case.",
        "Dear Mary and Bernard:",
        "The enclosed letter discussed the newspaper coverage.",
        "I read the letter again and realized that Sandra was a friend.",
        "The letter helped me understand why I had been fighting my legacy.",
        "My family's strength during my brother's death proved that I could never "
        "forget where I came from. I was proud of my heritage.",
        "On April 18 I wrote Sandra to tell her of my brother's death.",
        "Dear Mary and Bernard:",
    ]

    story = expand_story_window(paragraphs, 12).payload

    assert story["selection_policy"] == (
        "precise_seed_with_deterministic_source_links"
    )
    assert story["source_text_mode"] == "verbatim_selected_source_paragraphs"
    assert [row["paragraph_index"] for row in story["paragraphs"]] == [
        0,
        1,
        2,
        3,
        4,
        10,
        11,
        12,
        13,
    ]
    assert [row["role"] for row in story["paragraphs"][:5]] == [
        "referenced_event",
        "referenced_context",
        "referenced_context",
        "referenced_context",
        "referenced_context",
    ]
    assert story["segment_count"] == 2
    assert story["omissions"] == [
        {
            "after_paragraph_index": 4,
            "before_paragraph_index": 10,
            "paragraph_count": 5,
        }
    ]
    assert story["linked_context"]["strategy"] == "kinship_loss_reference_v1"
    assert story["linked_context"]["matched_kinship"] == "brother"
    assert "quoted article" not in story["text"]
    assert "enclosed letter" not in story["text"]
