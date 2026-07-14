"""
Three-stage matching engine:
  Stage 1: keyword pre-filter → fast elimination of irrelevant paragraphs
  Stage 2: semantic similarity scoring → cosine similarity against concept anchors
  Stage 3: narrative voice filter → keeps only first-person personal narratives

Stage 1 (KeywordMatcher): Fast substring search using the multilingual keyword
dictionary. Eliminates ~99% of irrelevant paragraphs.

Stage 2 (SemanticMatcher): Embeds candidate paragraphs with a multilingual
sentence-transformer and scores them against pre-computed concept anchors
via cosine similarity.

Stage 3 (NarrativeFilter): Checks for first-person pronouns and narrative
indicators across 20 languages. Eliminates dictionary definitions,
genealogy databases, commercial pages, and other non-personal text.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from concepts import CONCEPT_ANCHORS
from config import (
    DEVICE,
    ENCODING_BATCH_SIZE,
    MIN_NARRATIVE_INDICATORS,
    NARRATIVE_FILTER_ENABLED,
    SEMANTIC_MODEL_NAME,
    SEMANTIC_THRESHOLD,
)
from keywords import get_all_keywords_flat

if TYPE_CHECKING:
    from processor import Paragraph

logger = logging.getLogger(__name__)

# Short keywords (≤4 chars) need word-boundary matching to avoid
# substring false positives (e.g. "hem" matching inside "them").
_SHORT_KW_THRESHOLD = 4
_NO_BOUNDARY_SCRIPTS = ("CJK", "HIRAGANA", "KATAKANA", "HANGUL", "THAI")


def _needs_substring_matching(keyword: str) -> bool:
    """Return True for scripts where words are not reliably space-delimited."""
    for character in keyword:
        unicode_name = unicodedata.name(character, "")
        if any(script in unicode_name for script in _NO_BOUNDARY_SCRIPTS):
            return True
    return False


@dataclass
class Match:
    """A paragraph that passed all matching stages."""

    url: str
    warc_date: str
    text: str
    matched_keywords: list[str]
    semantic_score: float
    concept_match: str
    crawl_id: str = ""


class KeywordMatcher:
    """
    Fast keyword pre-filter.

    Scans paragraph text for any keyword from the flat multilingual dictionary.
    Case-insensitive matching. Short keywords (≤4 chars) use word-boundary
    matching to avoid substring false positives.
    """

    def __init__(self):
        all_kw = get_all_keywords_flat()

        # Split into short (regex) and long (substring) keywords
        self.long_keywords = []
        self.short_patterns = []

        for kw in all_kw:
            if len(kw) <= _SHORT_KW_THRESHOLD and not _needs_substring_matching(kw):
                # Use word boundary regex for short keywords
                try:
                    pattern = re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
                    self.short_patterns.append((kw, pattern))
                except re.error:
                    # Fallback to substring for regex-unfriendly patterns
                    self.long_keywords.append(kw)
            else:
                self.long_keywords.append(kw)

        logger.info(
            f"KeywordMatcher loaded {len(all_kw)} unique keywords "
            f"({len(self.long_keywords)} substring, {len(self.short_patterns)} word-boundary)"
        )

    def find_matches(self, text: str) -> list[str]:
        """
        Find all keywords present in the text.

        Returns:
            List of matched keywords (empty if none found)
        """
        text_lower = text.lower()
        found = []

        # Fast substring search for longer keywords
        for kw in self.long_keywords:
            if kw in text_lower:
                found.append(kw)

        # Regex word-boundary search for short keywords
        for kw, pattern in self.short_patterns:
            if pattern.search(text):
                found.append(kw)

        return found


class SemanticMatcher:
    """
    Semantic similarity scorer using a multilingual sentence-transformer.

    Encodes candidate paragraphs and compares them to pre-computed
    concept anchor embeddings via cosine similarity.
    """

    def __init__(self, encoding_batch_size: int = ENCODING_BATCH_SIZE):
        import numpy as np
        import torch
        from sentence_transformers import SentenceTransformer, util

        if DEVICE == "auto":
            if torch.cuda.is_available():
                device = "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
        else:
            device = DEVICE

        self.encoding_batch_size = encoding_batch_size
        self._numpy = np
        self._util = util
        self.device = device
        logger.info(f"Loading semantic model: {SEMANTIC_MODEL_NAME} on {device}")
        self.model = SentenceTransformer(SEMANTIC_MODEL_NAME, device=device)
        logger.info("Encoding concept anchors...")
        self.anchor_embeddings = self.model.encode(
            CONCEPT_ANCHORS,
            convert_to_tensor=True,
            show_progress_bar=False,
            device=device,
        )
        logger.info(f"Encoded {len(CONCEPT_ANCHORS)} concept anchors")

    def score_paragraphs(self, paragraphs: list[str]) -> list[tuple[float, str]]:
        """
        Score a batch of paragraphs against concept anchors.

        Args:
            paragraphs: List of paragraph texts to score

        Returns:
            List of (max_score, best_matching_concept) tuples
        """
        if not paragraphs:
            return []

        # Encode all paragraphs in a batch
        para_embeddings = self.model.encode(
            paragraphs,
            batch_size=self.encoding_batch_size,
            convert_to_tensor=True,
            show_progress_bar=False,
            device=self.device,
        )

        # Compute cosine similarity against all concept anchors
        # Shape: (num_paragraphs, num_anchors)
        cos_scores = self._util.cos_sim(para_embeddings, self.anchor_embeddings)

        results = []
        for i in range(len(paragraphs)):
            scores = cos_scores[i].cpu().numpy()
            max_idx = int(self._numpy.argmax(scores))
            max_score = float(scores[max_idx])
            best_concept = CONCEPT_ANCHORS[max_idx]
            results.append((max_score, best_concept))

        return results


class NarrativeFilter:
    """
    Stage 3: Narrative voice filter.

    Checks for first-person pronouns and narrative indicators across
    multiple languages. Filters out non-personal text like dictionary
    definitions, database records, marketing copy, and structured data.
    """

    # First-person pronouns and possessives across languages
    # These use word-boundary matching to avoid false positives
    _FIRST_PERSON_PATTERNS = [
        # English
        r"\bI\b",
        r"\bmy\b",
        r"\bme\b",
        r"\bmyself\b",
        r"\bmine\b",
        r"\bwe\b",
        r"\bour\b",
        r"\bourselves\b",
        # Spanish
        r"\byo\b",
        r"\bmi\b",
        r"\bmis\b",
        r"\bnosotros\b",
        r"\bnuestro\b",
        # French
        r"\bje\b",
        r"\bmon\b",
        r"\bma\b",
        r"\bmes\b",
        r"\bnous\b",
        r"\bnotre\b",
        # German
        r"\bich\b",
        r"\bmein\b",
        r"\bmeine\b",
        r"\bwir\b",
        r"\bunser\b",
        # Portuguese
        r"\beu\b",
        r"\bmeu\b",
        r"\bminha\b",
        r"\bnós\b",
        r"\bnosso\b",
        # Italian
        r"\bio\b",
        r"\bmio\b",
        r"\bmia\b",
        r"\bnoi\b",
        r"\bnostro\b",
        # Russian (no word boundaries for Cyrillic — use regex-free check below)
        # Turkish
        r"\bben\b",
        r"\bbenim\b",
        r"\bbiz\b",
        r"\bbizim\b",
        # Dutch
        r"\bik\b",
        r"\bmijn\b",
        r"\bwij\b",
        r"\bonze\b",
        # Polish
        r"\bja\b",
        r"\bmój\b",
        r"\bmoja\b",
        r"\bmy\b",
        r"\bnasz\b",
        # Swedish
        r"\bjag\b",
        r"\bmin\b",
        r"\bmitt\b",
        r"\bvi\b",
        r"\bvår\b",
        # Vietnamese
        r"\btôi\b",
        r"\bcủa tôi\b",
        r"\bchúng tôi\b",
        # Indonesian/Malay
        r"\bsaya\b",
        r"\bkami\b",
        r"\bkita\b",
    ]

    # Non-Latin script first-person markers (substring matching)
    _FIRST_PERSON_SUBSTRINGS = [
        # Chinese
        "我",
        "我的",
        "我们",
        # Japanese
        "私",
        "僕",
        "俺",
        "わたし",
        "ぼく",
        # Korean
        "나는",
        "내",
        "우리",
        "저는",
        "저의",
        # Arabic
        "أنا",
        "لي",
        "نحن",
        # Hindi
        "मैं",
        "मेरा",
        "मेरी",
        "हम",
        "हमारा",
        # Thai
        "ฉัน",
        "ผม",
        "ดิฉัน",
        "เรา",
        # Russian / Ukrainian
        "я ",
        " мой",
        " моя",
        " мое",
        " мои",
        " наш",
        "я ",
        " мій",
        " моя",
        " моє",
        " мої",
        " наш",
    ]

    # Narrative indicator phrases — strong signals of personal storytelling
    _NARRATIVE_PHRASES = [
        # English
        "I remember",
        "I recall",
        "I grew up",
        "when I was",
        "my mother",
        "my father",
        "my parents",
        "my family",
        "my grandmother",
        "my grandfather",
        "my grandparents",
        "I was born",
        "I moved",
        "I left",
        "I came",
        "I miss",
        "I feel",
        "I realized",
        "I discovered",
        "back home",
        "my childhood",
        "my hometown",
        "I always",
        "I used to",
        "I never forgot",
        # Chinese
        "我记得",
        "我从小",
        "小时候",
        "我的家",
        "我长大",
        "我出生",
        "我思念",
        "我怀念",
        # Portuguese
        "eu lembro",
        "eu cresci",
        "quando eu era",
        "minha mãe",
        "meu pai",
        "minha família",
    ]

    # Negative indicators — signals of non-narrative text
    _NEGATIVE_INDICATORS = [
        "wikipedia",
        "encyclopedia",
        "dictionary",
        "privacy policy",
        "terms of use",
        "terms of service",
        "all rights reserved",
        "search billions",
        "census records",
        "vital records",
        "family trees & communities",
        "immigration records",
        "military records",
        "directories & member lists",
        "court, land & probate",
        "finding aids",
        "site index",
        "login",
        "sign up",
        "password",
        "create account",
        "shopping cart",
        "add to cart",
        "buy now",
        "price:",
        "copyright ©",
        "all rights reserved",
        "contact us",
        "gedcom",
        "family tree builder",
        "myheritage",
        "ancestry.com",
        "genealogy",
        "hostelworld",
        "booking.com",
        "tripadvisor",
        "agoda",
        "shuttle",
        "airport transfer",
        "check-in",
        "check-out",
        "arrival instructions",
        "how to get there",
        "latitude",
        "longitude",
        "gps",
        "forget password",
        "sign in",
        "log in",
        "reserve",
        # Lyric Indicators
        "lyrics by",
        "songwriter",
        "produced by",
        "official video",
        "discography",
        "sheet music",
        "official audio",
        "feat.",
        "remix",
        "instrumental",
        "karaoke",
        # Ad/Commercial Indicators
        "special offer",
        "limited time",
        "save %",
        "off your order",
        "don't miss out",
        "apply now",
        "subscribe today",
        "free trial",
        "money back guarantee",
        "bestseller",
        "click here",
        "buy it now",
        "curriculum vitae",
        "professional portfolio",
        "employment history",
    ]

    # These words can indicate navigation or commercial pages, but they are
    # also common in real memories. They lower confidence without vetoing a
    # paragraph on their own.
    _SOFT_NEGATIVE_INDICATORS = [
        "directions",
        "map",
        "station",
        "stop",
        "train",
        "bus",
        "reception",
        "confirmation",
        "resume",
        "portfolio",
    ]

    # Sequences that often indicate site navigation or language pickers
    _NAVIGATION_SEQUENCES = [
        # Months (English)
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        # Months (Chinese)
        "1月",
        "2月",
        "3月",
        "4月",
        "5月",
        "6月",
        "7月",
        "8月",
        "9月",
        "10月",
        "11月",
        "12月",
        # Languages
        "english",
        "french",
        "german",
        "italian",
        "spanish",
        "czech",
        "danish",
        "dutch",
        "finnish",
        "norwegian",
        "polish",
        "portuguese",
        "swedish",
        "chinese",
        "korean",
        "japanese",
        "russian",
        "turkish",
        "vietnamese",
        "英语",
        "法语",
        "德语",
        "意大利语",
        "西班牙语",
        "捷克",
        "丹麦",
        "荷兰语",
        "芬兰语",
        "挪威语",
        "波兰语",
        "葡萄牙语",
        "瑞典语",
        "中文",
        "韩语",
        "日语",
        "俄语",
    ]

    _LYRIC_REGEX_PATTERNS = [
        r"\[Verse \d+\]",
        r"\[Chorus\]",
        r"\[Bridge\]",
        r"\[Outro\]",
        r"\[Intro\]",
        r"\(Chorus\)",
        r"\(Verse\)",
        r"\(Bridge\)",
    ]

    def __init__(self):
        # Pre-compile regex patterns for speed
        self._pronoun_patterns = []
        for pat in self._FIRST_PERSON_PATTERNS:
            try:
                self._pronoun_patterns.append(re.compile(pat, re.IGNORECASE))
            except re.error:
                pass

        self._narrative_phrases_lower = [p.lower() for p in self._NARRATIVE_PHRASES]
        self._negative_indicators_lower = [p.lower() for p in self._NEGATIVE_INDICATORS]
        self._soft_negative_indicators_lower = [p.lower() for p in self._SOFT_NEGATIVE_INDICATORS]
        self._nav_sequences_lower = [p.lower() for p in self._NAVIGATION_SEQUENCES]

        self._lyric_regexes = []
        for pat in self._LYRIC_REGEX_PATTERNS:
            try:
                self._lyric_regexes.append(re.compile(pat, re.IGNORECASE))
            except re.error:
                pass

        logger.info(
            f"NarrativeFilter loaded: {len(self._pronoun_patterns)} pronoun patterns, "
            f"{len(self._FIRST_PERSON_SUBSTRINGS)} substring markers, "
            f"{len(self._narrative_phrases_lower)} narrative phrases, "
            f"{len(self._negative_indicators_lower)} negative indicators, "
            f"{len(self._nav_sequences_lower)} nav markers"
        )

    def _is_navigation_or_form(self, text: str) -> bool:
        """
        Check if the text is likely site navigation, a language picker,
        or a complex form rather than a narrative paragraph.
        """
        text_lower = text.lower()

        # 1. Check for language/month picker density
        nav_count = 0
        for marker in self._nav_sequences_lower:
            if marker in text_lower:
                nav_count += 1

        if nav_count >= 5:
            return True

        # 2. Check for high symbol density (typical of menus/footer links)
        # Ratio of non-alphanumeric chars (excluding spaces) to total length
        symbols = sum(
            1 for character in text if not character.isalnum() and not character.isspace()
        )
        if len(text) > 100:
            symbol_ratio = symbols / len(text)
            if symbol_ratio > 0.15:
                return True

        # 3. Check for "Label: Value" or "Label [Input]" patterns
        # Look for many colons or brackets followed by short text
        if text.count(":") > 5 or text.count("|") > 5:
            return True

        return False

    def _is_repetitive(self, text: str) -> bool:
        """
        Check for repetitive structure within a paragraph.
        Common in song choruses or low-quality scraped text.
        """
        words = text.lower().split()
        if len(words) < 20:
            return False

        # Check for phrase repetition (3rd-word phrase overlap)
        if len(words) > 50:
            phrases = []
            for i in range(len(words) - 3):
                phrases.append(" ".join(words[i : i + 3]))

            if not phrases:
                return False

            unique_phrases = set(phrases)
            repetition_rate = 1.0 - (len(unique_phrases) / len(phrases))

            # If more than 25% of 3-word phrases are repeats, it's likely a chorus or spam
            if repetition_rate > 0.25:
                return True

        return False

    def count_indicators(self, text: str) -> int:
        """
        Count narrative voice indicators in a paragraph.

        Returns the total number of first-person pronouns, possessives,
        and narrative phrases found, adjusted by negative indicators.
        """
        count = 0
        text_lower = text.lower()

        # check negative indicators first (fail fast if strong signal)
        for neg in self._negative_indicators_lower:
            if neg in text_lower:
                return -50  # Hard penalty for institutional/commercial markers

        # Check regex-based lyric markers
        for pattern in self._lyric_regexes:
            if pattern.search(text):
                return -50  # Hard stop for explicit lyric markers

        # Check for repetitive structures
        if self._is_repetitive(text):
            return -50  # Hard stop for choruses

        # Check for navigation/form signals
        if self._is_navigation_or_form(text):
            return -50  # Hard stop for site navigation

        # Count repeated first-person usage, capped per form so spam cannot
        # dominate the score.
        for pattern in self._pronoun_patterns:
            count += min(3, sum(1 for _ in pattern.finditer(text)))

        # Prefer longer non-Latin markers and do not double-count overlapping
        # forms such as the Chinese equivalents of "I" and "my".
        occupied = [False] * len(text)
        for marker in sorted(set(self._FIRST_PERSON_SUBSTRINGS), key=len, reverse=True):
            start = 0
            marker_hits = 0
            while marker_hits < 3:
                index = text.find(marker, start)
                if index < 0:
                    break
                end = index + len(marker)
                if not any(occupied[index:end]):
                    count += 1
                    marker_hits += 1
                    occupied[index:end] = [True] * len(marker)
                start = index + max(1, len(marker))

        # Check narrative phrases (strong signal, worth more)
        for phrase in self._narrative_phrases_lower:
            if phrase in text_lower:
                count += 3  # Increased weight for strong narrative phrases

        for indicator in self._soft_negative_indicators_lower:
            if indicator in text_lower:
                count -= 2

        return count

    def passes(self, text: str, min_indicators: int) -> bool:
        """Check if a paragraph has enough narrative voice indicators."""
        return self.count_indicators(text) >= min_indicators


class HybridMatcher:
    """
    Orchestrates the three-stage matching pipeline.

    1. Keyword pre-filter (fast)
    2. Semantic similarity scoring (accurate)
    3. Narrative voice filter (personal stories only)
    """

    def __init__(
        self,
        threshold: float = SEMANTIC_THRESHOLD,
        encoding_batch_size: int = ENCODING_BATCH_SIZE,
        narrative_min_indicators: int = MIN_NARRATIVE_INDICATORS,
    ):
        self.threshold = threshold
        self.narrative_min_indicators = narrative_min_indicators
        self.keyword_matcher = KeywordMatcher()
        self.semantic_matcher = SemanticMatcher(encoding_batch_size)

        if NARRATIVE_FILTER_ENABLED:
            self.narrative_filter = NarrativeFilter()
        else:
            self.narrative_filter = None

        logger.info(
            f"HybridMatcher ready (threshold={self.threshold}, "
            f"narrative_filter={'ON' if self.narrative_filter else 'OFF'})"
        )

    def process_batch_stage2(self, batch: list[tuple[Paragraph, list[str]]]) -> list[Match]:
        """
        Run Stage 2 (Semantic) and Stage 3 (Narrative) on a batch of
        paragraphs that have already passed Stage 1 (Keyword).

        Args:
            batch: List of (Paragraph, list_of_keywords) tuples

        Returns:
            List of Match objects that passed all stages
        """
        if not batch:
            return []

        paragraphs = [b[0] for b in batch]
        keywords = [b[1] for b in batch]
        texts = [p.text for p in paragraphs]

        # Stage 2: Semantic similarity scoring
        scores = self.semantic_matcher.score_paragraphs(texts)

        # Filter by threshold
        semantic_matches = []
        for i, (score, concept) in enumerate(scores):
            if score >= self.threshold:
                semantic_matches.append((i, score, concept))

        if not semantic_matches:
            return []

        # Stage 3: Narrative voice filter
        matches = []
        if self.narrative_filter:
            for i, score, concept in semantic_matches:
                if self.narrative_filter.passes(paragraphs[i].text, self.narrative_min_indicators):
                    matches.append(
                        Match(
                            url=paragraphs[i].url,
                            warc_date=paragraphs[i].warc_date,
                            text=paragraphs[i].text,
                            matched_keywords=keywords[i],
                            semantic_score=score,
                            concept_match=concept,
                            crawl_id=paragraphs[i].crawl_id,
                        )
                    )
        else:
            for i, score, concept in semantic_matches:
                matches.append(
                    Match(
                        url=paragraphs[i].url,
                        warc_date=paragraphs[i].warc_date,
                        text=paragraphs[i].text,
                        matched_keywords=keywords[i],
                        semantic_score=score,
                        concept_match=concept,
                        crawl_id=paragraphs[i].crawl_id,
                    )
                )

        return matches

    def process_paragraphs(self, paragraphs: list[Paragraph]) -> list[Match]:
        """
        Run all matching stages on a list of paragraphs.

        Args:
            paragraphs: List of Paragraph objects from the processor

        Returns:
            List of Match objects that passed all stages
        """
        # Stage 1: Keyword pre-filter
        candidates = []
        candidate_keywords = []

        for para in paragraphs:
            kw_matches = self.keyword_matcher.find_matches(para.text)
            if kw_matches:
                candidates.append(para)
                candidate_keywords.append(kw_matches)

        if not candidates:
            return []

        logger.debug(
            f"Stage 1: {len(candidates)}/{len(paragraphs)} paragraphs "
            f"passed keyword filter ({len(candidates) / max(len(paragraphs), 1) * 100:.1f}%)"
        )

        # Stage 2: Semantic similarity scoring
        candidate_texts = [c.text for c in candidates]
        scores = self.semantic_matcher.score_paragraphs(candidate_texts)

        # Filter by threshold
        semantic_matches = []
        for i, (score, concept) in enumerate(scores):
            if score >= self.threshold:
                semantic_matches.append((i, score, concept))

        logger.debug(
            f"Stage 2: {len(semantic_matches)}/{len(candidates)} candidates "
            f"passed semantic threshold ({self.threshold})"
        )

        if not semantic_matches:
            return []

        # Stage 3: Narrative voice filter
        if self.narrative_filter:
            matches = []
            narrative_passed = 0
            for i, score, concept in semantic_matches:
                if self.narrative_filter.passes(candidates[i].text, self.narrative_min_indicators):
                    narrative_passed += 1
                    matches.append(
                        Match(
                            url=candidates[i].url,
                            warc_date=candidates[i].warc_date,
                            text=candidates[i].text,
                            matched_keywords=candidate_keywords[i],
                            semantic_score=score,
                            concept_match=concept,
                            crawl_id=candidates[i].crawl_id,
                        )
                    )

            logger.debug(
                f"Stage 3: {narrative_passed}/{len(semantic_matches)} candidates "
                "passed narrative voice filter "
                f"(min_indicators={self.narrative_min_indicators})"
            )
        else:
            # No narrative filter — pass everything from Stage 2
            matches = []
            for i, score, concept in semantic_matches:
                matches.append(
                    Match(
                        url=candidates[i].url,
                        warc_date=candidates[i].warc_date,
                        text=candidates[i].text,
                        matched_keywords=candidate_keywords[i],
                        semantic_score=score,
                        concept_match=concept,
                        crawl_id=candidates[i].crawl_id,
                    )
                )

        return matches
