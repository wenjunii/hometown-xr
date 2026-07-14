"""WET/ARC parsing and paragraph extraction with explicit processing stats."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Iterator

from warcio.archiveiterator import ArchiveIterator

from config import MAX_PARAGRAPH_LENGTH, MIN_PARAGRAPH_LENGTH


@dataclass
class Paragraph:
    url: str
    warc_date: str
    text: str
    crawl_id: str = ""


@dataclass
class ProcessingStats:
    """Mutable counters updated even when a source yields no candidates."""

    records_processed: int = 0
    interrupted: bool = False


class SourceReadError(RuntimeError):
    """Raised when a source record cannot be read completely."""


_REMOVE_CONTENT_TAGS = re.compile(
    r"<(script|style|noscript|iframe|svg|head)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_STRIP_TAGS = re.compile(r"<[^>]+>")
_MULTI_SPACE = re.compile(r"[ \t]+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")


def _html_to_text(html_content: str) -> str:
    text = _REMOVE_CONTENT_TAGS.sub(" ", html_content)
    text = re.sub(
        r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>|</blockquote>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    text = _STRIP_TAGS.sub(" ", text)
    text = html.unescape(text)
    text = _MULTI_SPACE.sub(" ", text)
    return _MULTI_NEWLINE.sub("\n\n", text).strip()


def extract_paragraphs_from_wet(
    stream,
    crawl_id: str = "",
    keyword_matcher=None,
    shutdown_event=None,
    stats: ProcessingStats | None = None,
) -> Iterator[tuple[Paragraph, list[str], int]]:
    """Yield keyword candidates from a modern WET stream."""
    stats = stats if stats is not None else ProcessingStats()

    for record in ArchiveIterator(stream):
        if shutdown_event and shutdown_event.is_set():
            stats.interrupted = True
            return
        if record.rec_type != "conversion":
            continue

        stats.records_processed += 1
        url = record.rec_headers.get_header("WARC-Target-URI") or ""
        warc_date = record.rec_headers.get_header("WARC-Date") or ""
        try:
            content = record.content_stream().read().decode("utf-8", errors="ignore")
        except Exception as exc:
            raise SourceReadError(f"Failed to read WET record {url}: {exc}") from exc

        if not content.strip():
            continue
        for paragraph, keywords in _extract_paras(
            content,
            url,
            warc_date,
            crawl_id,
            keyword_matcher,
            shutdown_event,
        ):
            yield paragraph, keywords, stats.records_processed

    if shutdown_event and shutdown_event.is_set():
        stats.interrupted = True


def extract_paragraphs_from_arc(
    stream,
    crawl_id: str = "",
    keyword_matcher=None,
    shutdown_event=None,
    stats: ProcessingStats | None = None,
) -> Iterator[tuple[Paragraph, list[str], int]]:
    """Yield keyword candidates from a legacy ARC stream."""
    stats = stats if stats is not None else ProcessingStats()

    for record in ArchiveIterator(stream, arc2warc=True):
        if shutdown_event and shutdown_event.is_set():
            stats.interrupted = True
            return
        if record.rec_type not in ("response", "resource"):
            continue

        content_type = ""
        if record.http_headers:
            content_type = record.http_headers.get_header("Content-Type") or ""
        if content_type and "html" not in content_type.lower():
            continue

        stats.records_processed += 1
        url = record.rec_headers.get_header("WARC-Target-URI") or ""
        warc_date = record.rec_headers.get_header("WARC-Date") or ""
        try:
            raw_content = record.content_stream().read()
            html_content = raw_content.decode("utf-8", errors="ignore")
        except Exception as exc:
            raise SourceReadError(f"Failed to read ARC record {url}: {exc}") from exc

        text_content = _html_to_text(html_content)
        if not text_content:
            continue
        for paragraph, keywords in _extract_paras(
            text_content,
            url,
            warc_date,
            crawl_id,
            keyword_matcher,
            shutdown_event,
        ):
            yield paragraph, keywords, stats.records_processed

    if shutdown_event and shutdown_event.is_set():
        stats.interrupted = True


def _extract_paras(
    content: str,
    url: str,
    warc_date: str,
    crawl_id: str = "",
    keyword_matcher=None,
    shutdown_event=None,
) -> Iterator[tuple[Paragraph, list[str]]]:
    for raw_paragraph in content.split("\n\n"):
        if shutdown_event and shutdown_event.is_set():
            return

        text = " ".join(raw_paragraph.split())
        if not MIN_PARAGRAPH_LENGTH <= len(text) <= MAX_PARAGRAPH_LENGTH:
            continue

        keywords: list[str] = []
        if keyword_matcher:
            keywords = keyword_matcher.find_matches(text)
            if not keywords:
                continue

        yield (
            Paragraph(url=url, warc_date=warc_date, text=text, crawl_id=crawl_id),
            keywords,
        )
