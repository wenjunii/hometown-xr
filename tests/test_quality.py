from quality import classify_content


def test_content_taxonomy_flags_known_non_story_sources_without_deleting_them():
    assert (
        classify_content("I remember home in every chorus.", "https://lyricsmode.com/song").category
        == "lyrics"
    )
    assert (
        classify_content("A poem about my hometown.", "https://poetrysoup.com/poem").category
        == "poetry"
    )
    adult = classify_content("A personal memory.", "https://asstr.org/story")
    assert adult.category == "adult_content"
    assert "sensitive" in adult.flags


def test_content_taxonomy_keeps_personal_prose_as_default_category():
    result = classify_content(
        "I remember the home where my grandparents raised me and the streets "
        "where our neighbors gathered every summer.",
        "https://example.test/memoir",
    )
    assert result.category == "personal_prose"
    assert not result.flags


def test_content_taxonomy_detects_commercial_and_genealogy_patterns():
    commercial = classify_content(
        "Add to cart today, buy now, and receive free shipping.",
        "https://shop.example.test/home",
    )
    assert commercial.category == "commercial"
    genealogy = classify_content(
        "This family tree says she was born on 1 May and married on 2 June.",
        "https://example.test/records",
    )
    assert genealogy.category == "genealogy"
