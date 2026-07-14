import json
from pathlib import Path

import pytest

from config import MIN_NARRATIVE_INDICATORS
from matcher import KeywordMatcher, NarrativeFilter

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "filter_cases.json").read_text(encoding="utf-8")
)


def test_cjk_keyword_matches_inside_unsegmented_text():
    matcher = KeywordMatcher()
    matches = matcher.find_matches("\u8fd9\u662f\u6211\u7684\u6545\u4e61\u6545\u4e8b")
    assert "\u6545\u4e61" in matches


def test_transport_word_does_not_veto_personal_story():
    narrative = NarrativeFilter()
    text = CASES[0]["text"]
    assert narrative.count_indicators(text) >= MIN_NARRATIVE_INDICATORS


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_labeled_multilingual_filter_cases(case):
    result = NarrativeFilter().passes(case["text"], MIN_NARRATIVE_INDICATORS)
    assert result is case["expected"]
