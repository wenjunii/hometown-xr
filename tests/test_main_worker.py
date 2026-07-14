from contextlib import contextmanager

import main
from crawl_catalog import CrawlInfo
from main import RuntimeSettings, process_file_worker
from matcher import Match
from output import OutputWriter
from processor import Paragraph


class ControlledEvent:
    def __init__(self):
        self.value = False

    def is_set(self):
        return self.value


class FakeMatcher:
    keyword_matcher = object()

    def process_batch_stage2(self, batch):
        paragraph = batch[0][0]
        return [
            Match(
                url=paragraph.url,
                warc_date=paragraph.warc_date,
                text=paragraph.text,
                matched_keywords=["home"],
                semantic_score=0.9,
                concept_match="home",
                crawl_id=paragraph.crawl_id,
            )
        ]


class FakeLanguageDetector:
    def detect(self, text):
        return "en", 0.99


def test_interrupted_worker_discards_partial_output(tmp_path, monkeypatch):
    event = ControlledEvent()
    writer = OutputWriter(tmp_path / "output")
    monkeypatch.setattr(main, "_worker_shutdown_event", event)
    monkeypatch.setattr(main, "_worker_matcher", FakeMatcher())
    monkeypatch.setattr(main, "_worker_lang_detector", FakeLanguageDetector())
    monkeypatch.setattr(main, "_worker_writer", writer)

    @contextmanager
    def fake_stream(file_path, crawl_info):
        del file_path, crawl_info
        yield object()

    def fake_extractor(stream, crawl_id, matcher, shutdown_event, stats):
        del stream, matcher, shutdown_event
        stats.records_processed = 5
        yield (
            Paragraph(
                url="https://example.test",
                warc_date="2026-01-01",
                text="I remember my home and my family from childhood.",
                crawl_id=crawl_id,
            ),
            ["home"],
            5,
        )
        event.value = True
        stats.interrupted = True

    monkeypatch.setattr(main, "stream_file", fake_stream)
    monkeypatch.setattr(main, "extract_paragraphs_from_wet", fake_extractor)
    settings = RuntimeSettings("3080", 1, 1, 1, 0.45, 0.5)
    crawl = CrawlInfo("crawl", "modern", "wet", "", "")

    result = process_file_worker("crawl-data/source.wet.gz", crawl, settings)
    assert result.status == "interrupted"
    assert writer.find_source_outputs("crawl-data/source.wet.gz") == []
