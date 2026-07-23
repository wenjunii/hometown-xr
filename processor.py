"""WET/ARC parsing and paragraph extraction with explicit processing stats."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Iterator

from warcio.archiveiterator import ArchiveIterator

from config import DOCUMENT_CONTEXT_CHARS, MAX_PARAGRAPH_LENGTH, MIN_PARAGRAPH_LENGTH
from record_identity import stable_document_id
from story_context import expand_story_window
from text_normalization import normalize_extracted_text


@dataclass
class Paragraph:
    url: str
    warc_date: str
    text: str
    crawl_id: str = ""
    source_file: str = ""
    document_id: str = ""
    paragraph_index: int = 0
    context_before: str = ""
    context_after: str = ""
    raw_text: str = ""
    story: dict = field(default_factory=dict)


@dataclass
class ProcessingStats:
    """Mutable counters updated even when a source yields no candidates."""

    records_processed: int = 0
    eligible_paragraphs: int = 0
    keyword_rejected: int = 0
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
    source_file: str = "",
    include_unmatched: bool = False,
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
            source_file,
            stats.records_processed,
            stats,
            include_unmatched,
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
    source_file: str = "",
    include_unmatched: bool = False,
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
            source_file,
            stats.records_processed,
            stats,
            include_unmatched,
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
    source_file: str = "",
    document_index: int = 0,
    stats: ProcessingStats | None = None,
    include_unmatched: bool = False,
) -> Iterator[tuple[Paragraph, list[str]]]:
    raw_paragraphs = [" ".join(value.split()) for value in content.split("\n\n")]
    raw_paragraphs = [value for value in raw_paragraphs if value]
    paragraphs = [
        " ".join(normalize_extracted_text(value).split()) for value in raw_paragraphs
    ]
    document_id = stable_document_id(
        crawl_id,
        source_file,
        url,
        warc_date,
        document_index,
    )
    for paragraph_index, text in enumerate(paragraphs):
        if shutdown_event and shutdown_event.is_set():
            return
        if not MIN_PARAGRAPH_LENGTH <= len(text) <= MAX_PARAGRAPH_LENGTH:
            continue
        if stats is not None:
            stats.eligible_paragraphs += 1

        keywords: list[str] = []
        if keyword_matcher:
            keywords = keyword_matcher.find_matches(text)
            if not keywords:
                if stats is not None:
                    stats.keyword_rejected += 1
                if not include_unmatched:
                    continue

        yield (
            Paragraph(
                url=url,
                warc_date=warc_date,
                text=text,
                crawl_id=crawl_id,
                source_file=source_file,
                document_id=document_id,
                paragraph_index=paragraph_index,
                context_before=(
                    paragraphs[paragraph_index - 1][-DOCUMENT_CONTEXT_CHARS:]
                    if paragraph_index > 0
                    else ""
                ),
                context_after=(
                    paragraphs[paragraph_index + 1][:DOCUMENT_CONTEXT_CHARS]
                    if paragraph_index + 1 < len(paragraphs)
                    else ""
                ),
                raw_text=(
                    raw_paragraphs[paragraph_index]
                    if raw_paragraphs[paragraph_index] != text
                    else ""
                ),
                story=expand_story_window(paragraphs, paragraph_index).payload,
            ),
            keywords,
        )
