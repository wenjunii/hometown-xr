import json
from pathlib import Path

import pytest

from config import MIN_NARRATIVE_INDICATORS
from matcher import HybridMatcher, KeywordMatcher, NarrativeFilter
from processor import Paragraph

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


def test_hard_boilerplate_negative_skips_semantic_inference():
    class SemanticMustNotRun:
        def score_paragraphs(self, paragraphs):
            raise AssertionError(f"unexpected semantic inference for {paragraphs}")

    matcher = object.__new__(HybridMatcher)
    matcher.threshold = 0.45
    matcher.narrative_min_indicators = MIN_NARRATIVE_INDICATORS
    matcher.narrative_filter = NarrativeFilter()
    matcher.semantic_matcher = SemanticMustNotRun()
    paragraph = Paragraph(
        "https://example.test/privacy",
        "2026-01-01",
        "Privacy policy and terms of service for our home property website.",
        "crawl",
        "source.wet.gz",
    )

    decisions = matcher.evaluate_batch_stage2([(paragraph, ["home"])])
    assert decisions[0].rejection_reason == "boilerplate_pre_filter"
