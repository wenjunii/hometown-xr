import numpy as np

from inference_cache import InferenceCache
from matcher import MatchDecision
from metrics import MetricsRecorder
from output import OutputWriter
from pipeline import CandidateBatch, InferenceService, SourceFinished
from processor import Paragraph
from runtime import RuntimeSettings


class CacheableMatcher:
    semantic_cache_namespace = "semantic:test"
    embedding_cache_namespace = "embedding:test"

    def __init__(self):
        self.encode_calls = 0
        self.cached_score_calls = 0

    def score_batch_stage2_with_embeddings(self, batch):
        self.encode_calls += 1
        return (
            [(0.9, "memories of home") for _item in batch],
            np.asarray([[1.0, 0.0] for _item in batch], dtype=np.float32),
        )

    def score_cached_embeddings(self, embeddings):
        self.cached_score_calls += 1
        return [(0.9, "memories of home") for _value in embeddings]

    def decisions_from_scores(self, batch, scores):
        return [
            MatchDecision(
                paragraph=paragraph,
                matched_keywords=keywords,
                semantic_score=score,
                concept_match=concept,
                narrative_score=12,
                accepted=True,
            )
            for (paragraph, keywords), (score, concept) in zip(batch, scores)
        ]

    def evaluate_batch_stage2(self, batch):
        raise AssertionError("the cache-aware path should be used")


class CacheableLanguageDetector:
    cache_namespace = "language:test"

    def __init__(self):
        self.predict_calls = 0

    def predict(self, text):
        assert text
        self.predict_calls += 1
        return "en", 0.99

    def apply_threshold(self, prediction):
        return prediction

    def detect(self, text):
        return self.apply_threshold(self.predict(text))


class NoopSampler:
    def observe(self, decisions, language_detector):
        del language_detector
        return len(decisions)


def test_inference_service_reuses_scores_embeddings_and_languages(tmp_path):
    settings = RuntimeSettings("3080", 1, 10, 10, 10, 0.45, 0.5)
    matcher = CacheableMatcher()
    detector = CacheableLanguageDetector()
    cache = InferenceCache(tmp_path / "cache.db")
    metrics = MetricsRecorder("3080", 1, 10, tmp_path / "metrics")
    service = InferenceService(
        settings,
        metrics,
        matcher=matcher,
        language_detector=detector,
        writer=OutputWriter(tmp_path / "output"),
        sampler=NoopSampler(),
        cache=cache,
    )
    text = "I remember my childhood home and the family who gave me belonging there."

    for number in (1, 2):
        source = f"source-{number}.wet.gz"
        paragraph = Paragraph(
            f"https://example.test/{number}",
            "2026-01-01",
            text,
            "crawl",
            source,
        )
        service.handle_candidate_batch(CandidateBatch(source, [(paragraph, ["home"])]))
        result = service.finish_source(SourceFinished(source, "completed"))
        assert result.matches_found == 1

    matcher.semantic_cache_namespace = "semantic:test:new-anchors"
    source = "source-3.wet.gz"
    paragraph = Paragraph(
        "https://example.test/3",
        "2026-01-01",
        text,
        "crawl",
        source,
    )
    service.handle_candidate_batch(CandidateBatch(source, [(paragraph, ["home"])]))
    assert service.finish_source(SourceFinished(source, "completed")).matches_found == 1

    assert matcher.encode_calls == 1
    assert matcher.cached_score_calls == 1
    assert detector.predict_calls == 1
    stats = cache.stats()
    assert stats["embeddings"] == 1
    assert stats["semantic_scores"] == 2
    assert stats["language_predictions"] == 1
    service.close()


def test_corrupt_cached_embedding_is_removed_and_treated_as_a_miss(tmp_path):
    with InferenceCache(tmp_path / "cache.db") as cache:
        cache.put_embeddings("model", {"text": np.asarray([1.0, 2.0], dtype=np.float32)})
        cache.conn.execute(
            "UPDATE embeddings SET vector = ? WHERE namespace = ? AND text_hash = ?",
            (b"not-zlib", "model", "text"),
        )
        cache.conn.commit()

        assert cache.get_embeddings("model", ["text"]) == {}
        assert cache.stats()["embeddings"] == 0
