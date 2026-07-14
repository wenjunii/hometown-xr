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

import hashlib
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
    SEMANTIC_MODEL_REVISION,
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
_ANCHOR_DIGEST = hashlib.sha256("\x1f".join(CONCEPT_ANCHORS).encode("utf-8")).hexdigest()[:16]


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
    source_file: str = ""
    narrative_score: int | None = None


@dataclass
class MatchDecision:
    """Complete filter decision for one keyword candidate."""

    paragraph: Paragraph
    matched_keywords: list[str]
    semantic_score: float
    concept_match: str
    narrative_score: int
    accepted: bool
    rejection_reason: str | None = None

    def to_match(self) -> Match:
        if not self.accepted:
            raise ValueError("a rejected decision cannot be converted to a match")
        return Match(
            url=self.paragraph.url,
            warc_date=self.paragraph.warc_date,
            text=self.paragraph.text,
            matched_keywords=self.matched_keywords,
            semantic_score=self.semantic_score,
            concept_match=self.concept_match,
            crawl_id=self.paragraph.crawl_id,
            source_file=self.paragraph.source_file,
            narrative_score=self.narrative_score,
        )


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

    def __init__(
        self,
        encoding_batch_size: int = ENCODING_BATCH_SIZE,
        precision: str = "fp32",
        adaptive_batching: bool = True,
    ):
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

        if precision not in {"fp32", "fp16"}:
            raise ValueError("precision must be fp32 or fp16")

        self._numpy = np
        self._torch = torch
        self._util = util
        self.device = device
        self.precision = precision
        if precision == "fp16" and not str(device).startswith("cuda"):
            logger.warning("FP16 was requested without CUDA; falling back to FP32")
            self.precision = "fp32"
        self.configured_batch_size = encoding_batch_size
        self.encoding_batch_size = encoding_batch_size
        self.adaptive_batching = adaptive_batching
        self.minimum_batch_size = min(8, encoding_batch_size)
        self._stable_batches = 0
        self._runtime_stats = {"oom_retries": 0, "batch_reductions": 0}
        self.embedding_cache_namespace = (
            f"{SEMANTIC_MODEL_NAME}@{SEMANTIC_MODEL_REVISION}:{self.precision}"
        )
        self.semantic_cache_namespace = (
            f"{self.embedding_cache_namespace}:anchors={_ANCHOR_DIGEST}"
        )
        logger.info(
            "Loading semantic model: %s@%s on %s (%s)",
            SEMANTIC_MODEL_NAME,
            SEMANTIC_MODEL_REVISION[:12],
            device,
            self.precision,
        )
        self.model = SentenceTransformer(
            SEMANTIC_MODEL_NAME,
            device=device,
            revision=SEMANTIC_MODEL_REVISION,
        )
        if self.precision == "fp16":
            self.model.half()
        logger.info("Encoding concept anchors...")
        self.anchor_embeddings = self.model.encode(
            CONCEPT_ANCHORS,
            convert_to_tensor=True,
            show_progress_bar=False,
            device=device,
        )
        logger.info(f"Encoded {len(CONCEPT_ANCHORS)} concept anchors")

    def _is_cuda_oom(self, exc: RuntimeError) -> bool:
        return str(self.device).startswith("cuda") and "out of memory" in str(exc).lower()

    def _adjust_for_memory_pressure(self) -> None:
        if not self.adaptive_batching or not str(self.device).startswith("cuda"):
            return
        try:
            free_bytes, total_bytes = self._torch.cuda.mem_get_info()
        except RuntimeError:
            return
        if total_bytes and free_bytes / total_bytes < 0.12:
            reduced = max(self.minimum_batch_size, self.encoding_batch_size // 2)
            if reduced < self.encoding_batch_size:
                self.encoding_batch_size = reduced
                self._runtime_stats["batch_reductions"] += 1
                self._stable_batches = 0
                logger.warning(
                    "GPU memory pressure reduced encoding batch size to %s",
                    self.encoding_batch_size,
                )

    def _recover_batch_size(self) -> None:
        if not self.adaptive_batching or self.encoding_batch_size >= self.configured_batch_size:
            return
        self._stable_batches += 1
        if self._stable_batches < 8:
            return
        restored = min(self.configured_batch_size, self.encoding_batch_size * 2)
        if restored > self.encoding_batch_size:
            self.encoding_batch_size = restored
            logger.info("Restored encoding batch size to %s", restored)
        self._stable_batches = 0

    def _encode_tensor(self, paragraphs: list[str]):
        self._adjust_for_memory_pressure()
        while True:
            try:
                embeddings = self.model.encode(
                    paragraphs,
                    batch_size=self.encoding_batch_size,
                    convert_to_tensor=True,
                    show_progress_bar=False,
                    device=self.device,
                )
                self._recover_batch_size()
                return embeddings
            except RuntimeError as exc:
                if not self._is_cuda_oom(exc) or not self.adaptive_batching:
                    raise
                reduced = max(self.minimum_batch_size, self.encoding_batch_size // 2)
                if reduced >= self.encoding_batch_size:
                    raise
                self.encoding_batch_size = reduced
                self._runtime_stats["oom_retries"] += 1
                self._runtime_stats["batch_reductions"] += 1
                self._stable_batches = 0
                self._torch.cuda.empty_cache()
                logger.warning(
                    "CUDA OOM recovered by reducing encoding batch size to %s",
                    reduced,
                )

    def encode_paragraphs(self, paragraphs: list[str]):
        """Encode text once and return cacheable CPU vectors."""
        if not paragraphs:
            return self._numpy.empty((0, 0), dtype=self._numpy.float32)
        embeddings = self._encode_tensor(paragraphs)
        dtype = self._numpy.float16 if self.precision == "fp16" else self._numpy.float32
        return embeddings.detach().cpu().numpy().astype(dtype, copy=False)

    def score_embeddings(self, embeddings) -> list[tuple[float, str]]:
        """Score cached or freshly encoded vectors against current anchors."""
        if len(embeddings) == 0:
            return []
        matrix = self._numpy.stack(embeddings)
        tensor = self._torch.as_tensor(
            matrix,
            device=self.device,
            dtype=self.anchor_embeddings.dtype,
        )
        cos_scores = self._util.cos_sim(tensor, self.anchor_embeddings)
        results = []
        for row in cos_scores:
            scores = row.detach().float().cpu().numpy()
            max_idx = int(self._numpy.argmax(scores))
            results.append((float(scores[max_idx]), CONCEPT_ANCHORS[max_idx]))
        return results

    def score_paragraphs_with_embeddings(self, paragraphs: list[str]):
        """Return semantic results together with reusable embeddings."""
        embeddings = self.encode_paragraphs(paragraphs)
        return self.score_embeddings(embeddings), embeddings

    def consume_runtime_stats(self) -> dict[str, int]:
        stats = {**self._runtime_stats, "encoding_batch_size": self.encoding_batch_size}
        self._runtime_stats = {"oom_retries": 0, "batch_reductions": 0}
        return stats

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

        results, _embeddings = self.score_paragraphs_with_embeddings(paragraphs)
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
        precision: str = "fp32",
        adaptive_batching: bool = True,
    ):
        self.threshold = threshold
        self.narrative_min_indicators = narrative_min_indicators
        self.keyword_matcher = KeywordMatcher()
        self.semantic_matcher = SemanticMatcher(
            encoding_batch_size,
            precision=precision,
            adaptive_batching=adaptive_batching,
        )

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
        return [
            decision.to_match()
            for decision in self.evaluate_batch_stage2(batch)
            if decision.accepted
        ]

    def evaluate_batch_stage2(
        self,
        batch: list[tuple[Paragraph, list[str]]],
    ) -> list[MatchDecision]:
        """Score every keyword candidate and retain filter diagnostics."""
        if not batch:
            return []

        prefiltered = self.prefilter_semantic_batch(batch)
        active_indexes = [index for index in range(len(batch)) if index not in prefiltered]
        scores = [prefiltered.get(index, (-1.0, "pre-filtered boilerplate")) for index in range(len(batch))]
        if active_indexes:
            active_scores = self.score_batch_stage2([batch[index] for index in active_indexes])
            for index, score in zip(active_indexes, active_scores):
                scores[index] = score
        return self.decisions_from_scores(batch, scores)

    @property
    def embedding_cache_namespace(self) -> str:
        return self.semantic_matcher.embedding_cache_namespace

    @property
    def semantic_cache_namespace(self) -> str:
        return self.semantic_matcher.semantic_cache_namespace

    def score_batch_stage2(
        self,
        batch: list[tuple[Paragraph, list[str]]],
    ) -> list[tuple[float, str]]:
        return self.semantic_matcher.score_paragraphs(
            [paragraph.text for paragraph, _keywords in batch]
        )

    def prefilter_semantic_batch(
        self,
        batch: list[tuple[Paragraph, list[str]]],
    ) -> dict[int, tuple[float, str]]:
        """Skip GPU work for hard narrative negatives that could never pass."""
        if self.narrative_filter is None:
            return {}
        return {
            index: (-1.0, "pre-filtered boilerplate")
            for index, (paragraph, _keywords) in enumerate(batch)
            if self.narrative_filter.count_indicators(paragraph.text) <= -50
        }

    def score_batch_stage2_with_embeddings(
        self,
        batch: list[tuple[Paragraph, list[str]]],
    ):
        return self.semantic_matcher.score_paragraphs_with_embeddings(
            [paragraph.text for paragraph, _keywords in batch]
        )

    def score_cached_embeddings(self, embeddings) -> list[tuple[float, str]]:
        return self.semantic_matcher.score_embeddings(embeddings)

    def decisions_from_scores(
        self,
        batch: list[tuple[Paragraph, list[str]]],
        scores: list[tuple[float, str]],
    ) -> list[MatchDecision]:
        if len(batch) != len(scores):
            raise ValueError("batch and semantic scores must have the same length")
        paragraphs = [item[0] for item in batch]
        keywords = [item[1] for item in batch]
        decisions: list[MatchDecision] = []

        for paragraph, matched_keywords, (score, concept) in zip(
            paragraphs,
            keywords,
            scores,
        ):
            narrative_score = (
                self.narrative_filter.count_indicators(paragraph.text)
                if self.narrative_filter
                else self.narrative_min_indicators
            )
            rejection_reason = None
            if score < 0 and concept == "pre-filtered boilerplate":
                rejection_reason = "boilerplate_pre_filter"
            elif score < self.threshold:
                rejection_reason = "semantic_threshold"
            elif self.narrative_filter and narrative_score < self.narrative_min_indicators:
                rejection_reason = "narrative_threshold"

            decisions.append(
                MatchDecision(
                    paragraph=paragraph,
                    matched_keywords=matched_keywords,
                    semantic_score=score,
                    concept_match=concept,
                    narrative_score=narrative_score,
                    accepted=rejection_reason is None,
                    rejection_reason=rejection_reason,
                )
            )

        return decisions

    def process_paragraphs(self, paragraphs: list[Paragraph]) -> list[Match]:
        """Run all three matching stages on a list of paragraphs."""
        candidates = []
        for paragraph in paragraphs:
            keywords = self.keyword_matcher.find_matches(paragraph.text)
            if keywords:
                candidates.append((paragraph, keywords))
        return self.process_batch_stage2(candidates)
