import pytest

import processor
from processor import (
    ProcessingStats,
    SourceReadError,
    _extract_paras,
    extract_paragraphs_from_wet,
)


class Headers:
    def __init__(self, values=None):
        self.values = values or {}

    def get_header(self, name):
        return self.values.get(name)


class Content:
    def __init__(self, data=b"short content"):
        self.data = data

    def read(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Record:
    rec_type = "conversion"
    rec_headers = Headers({"WARC-Target-URI": "https://example.test", "WARC-Date": "2026-01-01"})

    def __init__(self, data=b"short content"):
        self.content = Content(data)

    def content_stream(self):
        return self.content


class NoKeywords:
    def find_matches(self, text):
        return []


class AllKeywords:
    def find_matches(self, text):
        return ["home"]


def test_record_count_includes_records_without_keyword_candidates(monkeypatch):
    monkeypatch.setattr(processor, "ArchiveIterator", lambda stream: [Record(), Record()])
    stats = ProcessingStats()
    results = list(
        extract_paragraphs_from_wet(object(), "crawl", keyword_matcher=NoKeywords(), stats=stats)
    )
    assert results == []
    assert stats.records_processed == 2


def test_archive_iterator_errors_are_not_silently_swallowed(monkeypatch):
    def broken_iterator(stream):
        del stream
        yield Record()
        raise ValueError("truncated archive")

    monkeypatch.setattr(processor, "ArchiveIterator", broken_iterator)
    with pytest.raises(ValueError, match="truncated archive"):
        list(extract_paragraphs_from_wet(object(), stats=ProcessingStats()))


def test_record_read_error_fails_the_source(monkeypatch):
    monkeypatch.setattr(
        processor,
        "ArchiveIterator",
        lambda stream: [Record(OSError("broken stream"))],
    )
    with pytest.raises(SourceReadError, match="broken stream"):
        list(extract_paragraphs_from_wet(object(), stats=ProcessingStats()))


def test_paragraphs_keep_stable_document_context():
    first = "First context " + "a" * 160
    middle = "Middle home story " + "b" * 160
    last = "Last context " + "c" * 160
    rows = list(
        _extract_paras(
            "\n\n".join((first, middle, last)),
            "https://example.test/story",
            "2026-01-01",
            "crawl",
            AllKeywords(),
            source_file="source.wet.gz",
            document_index=7,
        )
    )
    paragraph = rows[1][0]
    assert paragraph.paragraph_index == 1
    assert paragraph.context_before == first
    assert paragraph.context_after == last
    assert len(paragraph.document_id) == 64
