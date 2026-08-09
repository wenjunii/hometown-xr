import gzip
import json
import multiprocessing
from io import BytesIO

from warcio.warcwriter import WARCWriter

from crawl_catalog import CrawlInfo
from matcher import MatchDecision
from metrics import MetricsRecorder
from output import OutputWriter
from pipeline import (
    CandidateBatch,
    ExtractionPipeline,
    InferenceService,
    ShadowBatch,
    SourceFinished,
    _AdaptiveSourceThrottle,
)
from processor import Paragraph
from progress import ProgressTracker
from runtime import RuntimeSettings


class FakeMatcher:
    def evaluate_batch_stage2(self, batch):
        return [
            MatchDecision(
                paragraph=paragraph,
                matched_keywords=keywords,
                semantic_score=0.9,
                concept_match="memories of home",
                narrative_score=12,
                accepted=True,
            )
            for paragraph, keywords in batch
        ]


class FakeLanguageDetector:
    def detect(self, text):
        assert text
        return "en", 0.99


class FailingMatcher:
    def evaluate_batch_stage2(self, batch):
        del batch
        raise RuntimeError("simulated CUDA failure")


class NoopSampler:
    def observe(self, decisions, language_detector):
        del language_detector
        return len(decisions)

    def observe_shadow(
        self,
        paragraphs,
        population_size,
        language_detector,
        source_probability=1.0,
    ):
        del population_size, language_detector, source_probability
        return len(paragraphs)


def _settings():
    return RuntimeSettings("3080", 1, 10, 10, 10, 0.45, 0.5)


def _write_wet(path):
    paragraph = (
        "I remember my childhood home in my hometown, where my family gathered "
        "every summer. We told stories in the kitchen, played in the garden, and "
        "felt that returning to those familiar rooms restored our sense of belonging."
    )
    with path.open("wb") as handle:
        writer = WARCWriter(handle, gzip=True)
        record = writer.create_warc_record(
            "https://example.test/story",
            "conversion",
            payload=BytesIO(paragraph.encode("utf-8")),
            warc_headers_dict={"WARC-Date": "2026-01-01T00:00:00Z"},
        )
        writer.write_record(record)


def test_source_throttle_reduces_on_503_and_recovers_gradually():
    now = [100.0]
    throttle = _AdaptiveSourceThrottle(
        4,
        recovery_successes=2,
        circuit_base_seconds=30,
        clock=lambda: now[0],
    )
    assert throttle.available_slots(0) == 4
    assert throttle.observe("failed", "HTTP 503 service unavailable") == (4, 2)
    assert throttle.cooldown_remaining() == 30
    assert throttle.available_slots(1) == 0
    now[0] += 30
    assert throttle.available_slots(1) == 1
    assert throttle.observe("completed") is None
    assert throttle.observe("completed") == (2, 3)


def test_synthetic_wet_runs_through_spawned_parser_to_atomic_output(tmp_path):
    wet_path = tmp_path / "synthetic.warc.wet.gz"
    _write_wet(wet_path)
    tracker = ProgressTracker(tmp_path / "progress.db")
    tracker.initialize_paths([str(wet_path)], "crawl")
    writer = OutputWriter(tmp_path / "output")
    metrics = MetricsRecorder("3080", 1, 10, tmp_path / "metrics")
    service = InferenceService(
        _settings(),
        metrics,
        matcher=FakeMatcher(),
        language_detector=FakeLanguageDetector(),
        writer=writer,
        sampler=NoopSampler(),
    )
    context = multiprocessing.get_context("spawn")
    crawl = CrawlInfo("crawl", "modern", "wet", "", "")

    with ExtractionPipeline(_settings(), context, metrics, service=service) as pipeline:
        completed, matches = pipeline.process_crawl(tracker, crawl, 1)

    assert (completed, matches) == (1, 1)
    output_path = writer.find_source_outputs(str(wet_path))[0]
    with gzip.open(output_path, "rt", encoding="utf-8") as handle:
        record = json.loads(handle.readline())
    assert record["paragraph"].startswith("I remember my childhood home")
    assert record["record_id"]
    assert writer.verify_source(str(wet_path)) == []


def test_interrupted_source_discards_staged_output(tmp_path):
    source = "crawl-data/interrupted.warc.wet.gz"
    writer = OutputWriter(tmp_path / "output")
    metrics = MetricsRecorder("3080", 1, 10, tmp_path / "metrics")
    service = InferenceService(
        _settings(),
        metrics,
        matcher=FakeMatcher(),
        language_detector=FakeLanguageDetector(),
        writer=writer,
        sampler=NoopSampler(),
    )
    paragraph = Paragraph(
        "https://example.test",
        "2026-01-01",
        "I remember my home and my family from childhood.",
        "crawl",
        source,
    )
    service.handle_candidate_batch(CandidateBatch(source, [(paragraph, ["home"])]))
    result = service.finish_source(SourceFinished(source, "interrupted"))

    assert result.status == "interrupted"
    assert writer.find_source_outputs(source) == []
    assert not writer.manifest_path(source).exists()


def test_process_pool_recovery_interrupt_discards_output_for_immediate_retry(tmp_path):
    source = "crawl-data/pool-retry.warc.wet.gz"
    writer = OutputWriter(tmp_path / "output")
    metrics = MetricsRecorder("3080", 1, 10, tmp_path / "metrics")
    service = InferenceService(
        _settings(),
        metrics,
        matcher=FakeMatcher(),
        language_detector=FakeLanguageDetector(),
        writer=writer,
        sampler=NoopSampler(),
    )
    paragraph = Paragraph(
        "https://example.test",
        "2026-01-01",
        "I remember my home and my family from childhood.",
        "crawl",
        source,
    )
    service.handle_candidate_batch(CandidateBatch(source, [(paragraph, ["home"])]))

    result = service.interrupt_source(source, "process pool restarted")

    assert result.status == "interrupted"
    assert writer.find_source_outputs(source) == []
    assert not writer.manifest_path(source).exists()


def test_shadow_batch_passes_source_probability_to_sampler(tmp_path):
    class CapturingSampler(NoopSampler):
        def __init__(self):
            self.call = None

        def observe_shadow(
            self,
            paragraphs,
            population_size,
            language_detector,
            source_probability=1.0,
        ):
            self.call = (paragraphs, population_size, source_probability)
            return len(paragraphs)

    sampler = CapturingSampler()
    metrics = MetricsRecorder("3080", 1, 10, tmp_path / "metrics")
    service = InferenceService(
        _settings(),
        metrics,
        matcher=FakeMatcher(),
        language_detector=FakeLanguageDetector(),
        writer=OutputWriter(tmp_path / "output"),
        sampler=sampler,
    )
    paragraph = Paragraph("https://example.test", "2026-01-01", "sample")

    service.handle_shadow_batch(ShadowBatch("source", [paragraph], 10, 0.2))

    assert sampler.call == ([paragraph], 10, 0.2)
    service.close()


def test_inference_failure_aborts_sources_and_releases_workers(tmp_path):
    wet_path = tmp_path / "failing.warc.wet.gz"
    _write_wet(wet_path)
    tracker = ProgressTracker(tmp_path / "progress.db")
    tracker.initialize_paths([str(wet_path)], "crawl")
    writer = OutputWriter(tmp_path / "output")
    metrics = MetricsRecorder("3080", 1, 10, tmp_path / "metrics")
    service = InferenceService(
        _settings(),
        metrics,
        matcher=FailingMatcher(),
        language_detector=FakeLanguageDetector(),
        writer=writer,
        sampler=NoopSampler(),
    )
    context = multiprocessing.get_context("spawn")
    crawl = CrawlInfo("crawl", "modern", "wet", "", "")

    with ExtractionPipeline(_settings(), context, metrics, service=service) as pipeline:
        completed, matches = pipeline.process_crawl(tracker, crawl, 1)

    assert (completed, matches) == (0, 0)
    assert tracker.get_summary("crawl")["failed"] == 1
    assert writer.find_source_outputs(str(wet_path)) == []
