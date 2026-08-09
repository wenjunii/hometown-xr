import json
from pathlib import Path

import pytest

from config import MIN_NARRATIVE_INDICATORS
from keywords import KEYWORDS_BY_LANGUAGE
from matcher import HybridMatcher, KeywordMatcher, NarrativeFilter
from processor import Paragraph

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "filter_cases.json").read_text(encoding="utf-8")
)

FIRST_PERSON_COVERAGE = {
    "ar": "\u0623\u0646\u0627 \u0644\u064a \u0646\u062d\u0646 \u0623\u0646\u0627 \u0644\u064a \u0646\u062d\u0646 \u0623\u0646\u0627 \u0644\u064a \u0646\u062d\u0646",
    "de": "ich mein meine wir unser ich mein meine wir unser",
    "en": "I my me we our I my me we our",
    "es": "yo mi mis nosotros nuestro yo mi mis nosotros nuestro",
    "fr": "je mon ma mes nous notre je mon ma mes nous notre",
    "hi": "\u092e\u0948\u0902 \u092e\u0947\u0930\u093e \u092e\u0947\u0930\u0940 \u0939\u092e \u0939\u092e\u093e\u0930\u093e \u092e\u0948\u0902 \u092e\u0947\u0930\u093e \u092e\u0947\u0930\u0940",
    "id": "saya kami kita saya kami kita saya kami kita",
    "it": "io mio mia noi nostro io mio mia noi nostro",
    "ja": "\u79c1 \u50d5 \u4ffa \u308f\u305f\u3057 \u307c\u304f \u79c1 \u50d5 \u4ffa",
    "ko": "\ub098\ub294 \ub0b4 \uc6b0\ub9ac \uc800\ub294 \uc800\uc758 \ub098\ub294 \ub0b4 \uc6b0\ub9ac",
    "nl": "ik mijn wij onze ik mijn wij onze",
    "pl": "ja m\u00f3j moja my nasz ja m\u00f3j moja my nasz",
    "pt": "eu meu minha n\u00f3s nosso eu meu minha n\u00f3s nosso",
    "ru": "\u044f \u043c\u043e\u0439 \u043c\u043e\u044f \u043c\u043e\u0435 \u043c\u043e\u0438 \u043d\u0430\u0448 \u044f \u043c\u043e\u0439 \u043c\u043e\u044f",
    "sv": "jag min mitt vi v\u00e5r jag min mitt vi v\u00e5r",
    "th": "\u0e09\u0e31\u0e19 \u0e1c\u0e21 \u0e14\u0e34\u0e09\u0e31\u0e19 \u0e40\u0e23\u0e32 \u0e09\u0e31\u0e19 \u0e1c\u0e21 \u0e14\u0e34\u0e09\u0e31\u0e19 \u0e40\u0e23\u0e32",
    "tr": "ben benim biz bizim ben benim biz bizim",
    "uk": "\u044f \u043c\u0456\u0439 \u043c\u043e\u044f \u043c\u043e\u0454 \u043c\u043e\u0457 \u043d\u0430\u0448 \u044f \u043c\u0456\u0439 \u043c\u043e\u044f",
    "vi": "t\u00f4i c\u1ee7a t\u00f4i ch\u00fang t\u00f4i t\u00f4i c\u1ee7a t\u00f4i ch\u00fang t\u00f4i",
    "zh": "\u6211 \u6211\u7684 \u6211\u4eec \u6211 \u6211\u7684 \u6211\u4eec \u6211 \u6211\u7684 \u6211\u4eec",
}


def test_cjk_keyword_matches_inside_unsegmented_text():
    matcher = KeywordMatcher()
    matches = matcher.find_matches("\u8fd9\u662f\u6211\u7684\u6545\u4e61\u6545\u4e8b")
    assert "\u6545\u4e61" in matches


@pytest.mark.parametrize("language", sorted(FIRST_PERSON_COVERAGE))
def test_every_keyword_language_has_narrative_and_keyword_regression_coverage(language):
    assert set(FIRST_PERSON_COVERAGE) == set(KEYWORDS_BY_LANGUAGE)
    keyword = KEYWORDS_BY_LANGUAGE[language][0]
    text = f"{FIRST_PERSON_COVERAGE[language]} {keyword}"
    assert keyword.lower() in KeywordMatcher().find_matches(text)
    assert NarrativeFilter().passes(text, MIN_NARRATIVE_INDICATORS)


def test_transport_word_does_not_veto_personal_story():
    narrative = NarrativeFilter()
    text = CASES[0]["text"]
    assert narrative.count_indicators(text) >= MIN_NARRATIVE_INDICATORS


def test_cyrillic_first_person_markers_are_case_insensitive():
    text = "\u042f \u043c\u043e\u0439 \u0434\u043e\u043c \u0438 \u043c\u043e\u0439 \u0440\u0430\u0439\u043e\u043d. \u042f \u043c\u043e\u044f \u0441\u0435\u043c\u044c\u044f. \u042f \u043c\u043e\u0438 \u0432\u043e\u0441\u043f\u043e\u043c\u0438\u043d\u0430\u043d\u0438\u044f. \u042f \u043d\u0430\u0448 \u0433\u043e\u0440\u043e\u0434."
    assert NarrativeFilter().passes(text, MIN_NARRATIVE_INDICATORS)


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
