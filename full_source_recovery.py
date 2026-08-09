"""Recover complete Common Crawl documents for bounded story matches.

The paragraph matcher remains the discovery authority.  This module performs a
second, resumable pass against the Common Crawl index, retains the complete
captured document once, and derives conservative narrative units without
generating or completing source text.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

import requests
from warcio.archiveiterator import ArchiveIterator

from config import (
    CC_BASE_URL,
    FULL_SOURCE_EXPORTS_DIR,
    FULL_SOURCE_MAX_NARRATIVE_CHARS,
    FULL_SOURCE_MAX_NARRATIVE_PARAGRAPHS,
    FULL_SOURCE_MAX_WORKERS,
    FULL_SOURCE_RECOVERY_VERSION,
    FULL_SOURCE_WHOLE_DOCUMENT_CHARS,
    FULL_SOURCE_WORKERS,
    FULL_SOURCES_DIR,
    HTTP_TIMEOUT,
    STORIES_DIR,
)
from deterministic_gzip import gzip_binary_writer
from downloader import _make_session
from record_identity import normalize_text, normalize_url
from story_enrichment import iter_story_records
from text_normalization import normalize_extracted_text

FULL_SOURCE_SCHEMA_VERSION = 1
_RECORDS_DIR = "_records"
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
_SKIP_TAGS = {"head", "iframe", "noscript", "script", "style", "svg"}
_HEADING = re.compile(
    r"^(?:chapter|part|section|book|appendix|contents?|letter|entry)\b",
    re.IGNORECASE,
)
_DATE_HEADING = re.compile(
    r"^(?:[A-Z][A-Za-z .'-]+,\s+)?(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|"
    r"Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:,\s+|\s+)\d{4}",
    re.IGNORECASE,
)
_GATED_MARKERS = (
    "please login",
    "please log in",
    "sign up to continue",
    "subscribe to continue",
    "preview only",
    "read more after the jump",
)
_CLOSING_PUNCTUATION = tuple(".!?\u2026\"'\u201d\u2019)]}")


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        del attrs
        lowered = tag.casefold()
        if lowered in _SKIP_TAGS:
            self.skip_depth += 1
        elif not self.skip_depth and lowered in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in _SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and lowered in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        return _clean_document_text("".join(self.parts))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _capture_month(warc_date: str) -> str:
    digits = re.sub(r"[^0-9]", "", warc_date)
    return f"{digits[:4]}-{digits[4:6]}" if len(digits) >= 6 else ""


def _capture_key(row: dict) -> str:
    payload = "\x1f".join(
        (
            str(row.get("crawl_id", "")),
            normalize_url(str(row.get("url", ""))),
            str(row.get("warc_date", "")),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_path(capture_key: str, root: str | Path = FULL_SOURCES_DIR) -> Path:
    return Path(root) / _RECORDS_DIR / f"{capture_key}.json.gz"


def _write_gzip_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as raw:
        with gzip_binary_writer(raw) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
    os.replace(temporary, path)


def _read_gzip_json(path: Path) -> dict | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            value = json.loads(handle.readline())
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if _valid_recovery(value) else None


def _valid_recovery(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    document = value.get("document")
    units = value.get("narrative_units")
    return bool(
        value.get("schema_version") == FULL_SOURCE_SCHEMA_VERSION
        and value.get("recovery_version") == FULL_SOURCE_RECOVERY_VERSION
        and value.get("capture_key")
        and isinstance(document, dict)
        and document.get("text")
        and document.get("text_sha256") == _sha256_text(str(document["text"]))
        and isinstance(units, list)
        and units
        and all(
            isinstance(unit, dict)
            and unit.get("story_id")
            and unit.get("narrative_text")
            and unit.get("narrative_sha256")
            == _sha256_text(str(unit["narrative_text"]))
            for unit in units
        )
    )


def _portable_records(export_root: str | Path) -> dict[str, dict]:
    grouped: dict[str, dict] = {}
    for path in sorted(Path(export_root).glob("*/stories_full.jsonl.gz")):
        try:
            handle = gzip.open(path, "rt", encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (UnicodeError, json.JSONDecodeError):
                    continue
                capture_key = str(row.get("capture_key", ""))
                unit = row.get("narrative_unit")
                document = row.get("document")
                if not capture_key or not isinstance(unit, dict) or not isinstance(document, dict):
                    continue
                record = grouped.setdefault(
                    capture_key,
                    {
                        "schema_version": FULL_SOURCE_SCHEMA_VERSION,
                        "recovery_version": row.get("recovery_version"),
                        "generated_text": False,
                        "capture_key": capture_key,
                        "capture_family_id": row.get("capture_family_id"),
                        "crawl_id": row.get("crawl_id"),
                        "source_file": row.get("source_file"),
                        "url": row.get("url"),
                        "warc_date": row.get("warc_date"),
                        "indexed_capture": row.get("indexed_capture", {}),
                        "document": document,
                        "narrative_units": [],
                    },
                )
                identity = (unit.get("story_id"), unit.get("record_id"))
                if all(
                    (existing.get("story_id"), existing.get("record_id")) != identity
                    for existing in record["narrative_units"]
                ):
                    record["narrative_units"].append(unit)
    return {
        key: record
        for key, record in grouped.items()
        if _valid_recovery(record)
    }


def iter_full_source_records(
    root: str | Path = FULL_SOURCES_DIR,
    export_root: str | Path | None = FULL_SOURCE_EXPORTS_DIR,
):
    seen = set()
    for path in sorted((Path(root) / _RECORDS_DIR).glob("*.json.gz")):
        value = _read_gzip_json(path)
        if value is not None:
            seen.add(value["capture_key"])
            yield value
    if export_root is not None:
        for key, value in sorted(_portable_records(export_root).items()):
            if key not in seen:
                yield value


def _target_groups(
    stories_dir: str | Path,
    month: str,
) -> dict[str, list[dict]]:
    """Return one canonical capture for every normal exported story in a month."""
    by_story: dict[str, list[dict]] = defaultdict(list)
    for row in iter_story_records(stories_dir):
        by_story[str(row["story_id"])].append(row)
    groups: dict[str, list[dict]] = defaultdict(list)
    for rows in by_story.values():
        rows.sort(
            key=lambda row: (
                -float((row.get("seed") or {}).get("semantic_score", 0.0)),
                str(row.get("warc_date", "")),
                str(row.get("record_id", "")),
            )
        )
        representative = rows[0]
        if not (representative.get("story") or {}).get("story_length_ready"):
            continue
        if _capture_month(str(representative.get("warc_date", ""))) == month:
            groups[_capture_key(representative)].append(representative)
    for rows in groups.values():
        rows.sort(key=lambda item: (str(item["story_id"]), str(item["record_id"])))
    return dict(sorted(groups.items()))


def plan_full_source_recovery(
    month: str,
    *,
    stories_dir: str | Path = STORIES_DIR,
    full_sources_dir: str | Path = FULL_SOURCES_DIR,
    export_root: str | Path | None = FULL_SOURCE_EXPORTS_DIR,
    limit: int | None = 10,
) -> dict:
    """Plan pending capture recovery without making network requests."""
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("month must use YYYY-MM")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    groups = _target_groups(stories_dir, month)
    recovered = {
        row["capture_key"]: row
        for row in iter_full_source_records(full_sources_dir, export_root)
    }
    pending = [key for key in groups if key not in recovered]
    selection = pending if limit is None else pending[:limit]
    return {
        "schema_version": FULL_SOURCE_SCHEMA_VERSION,
        "recovery_version": FULL_SOURCE_RECOVERY_VERSION,
        "capture_month": month,
        "target_captures": len(groups),
        "target_story_records": sum(len(rows) for rows in groups.values()),
        "recovered_captures": sum(key in recovered for key in groups),
        "pending_captures": len(pending),
        "selected_captures": len(selection),
        "network_request_ceiling": len(selection) * 2,
        "selection": [
            {
                "capture_key": key,
                "crawl_id": groups[key][0]["crawl_id"],
                "url": groups[key][0]["url"],
                "warc_date": groups[key][0]["warc_date"],
                "story_records": len(groups[key]),
            }
            for key in selection
        ],
    }


def _timestamp_distance(left: str, right: str) -> float:
    def parse(value: str) -> datetime:
        digits = re.sub(r"[^0-9]", "", value)[:14].ljust(14, "0")
        return datetime.strptime(digits, "%Y%m%d%H%M%S")

    try:
        return abs((parse(left) - parse(right)).total_seconds())
    except ValueError:
        return float("inf")


def lookup_index_capture(
    session: requests.Session,
    *,
    crawl_id: str,
    url: str,
    warc_date: str,
) -> dict:
    endpoint = f"https://index.commoncrawl.org/{crawl_id}-index"
    response = session.get(
        endpoint,
        params={"url": url, "output": "json"},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    candidates = []
    for line in response.text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("filename") and row.get("offset") and row.get("length"):
            candidates.append(row)
    if not candidates:
        raise LookupError(f"Common Crawl index returned no capture for {url}")
    candidates.sort(
        key=lambda row: (
            _timestamp_distance(str(row.get("timestamp", "")), warc_date),
            str(row.get("filename", "")),
            int(row.get("offset", 0)),
        )
    )
    return candidates[0]


def fetch_indexed_payload(session: requests.Session, index_row: dict) -> dict:
    offset = int(index_row["offset"])
    length = int(index_row["length"])
    response = session.get(
        f"{CC_BASE_URL}{index_row['filename']}",
        headers={"Range": f"bytes={offset}-{offset + length - 1}"},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    record = next(ArchiveIterator(io.BytesIO(response.content)))
    payload = record.content_stream().read()
    content_type = ""
    if record.http_headers:
        content_type = record.http_headers.get_header("Content-Type") or ""
    return {
        "payload": payload,
        "content_type": content_type,
        "warc_target_uri": record.rec_headers.get_header("WARC-Target-URI") or "",
        "warc_date": record.rec_headers.get_header("WARC-Date") or "",
    }


def _decode_payload(payload: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
    candidates = [charset_match.group(1)] if charset_match else []
    candidates.extend(("utf-8", "windows-1252"))
    for encoding in candidates:
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _clean_document_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = []
    for raw in re.split(r"\n\s*\n", value):
        cleaned = " ".join(normalize_extracted_text(raw).split())
        if cleaned:
            paragraphs.append(cleaned)
    return "\n\n".join(paragraphs)


def extract_visible_document(payload: bytes, content_type: str) -> str:
    decoded = _decode_payload(payload, content_type)
    if "html" in content_type.casefold() or re.search(
        r"<\s*(?:html|body|article|p|div)\b", decoded[:8192], re.IGNORECASE
    ):
        parser = _VisibleTextParser()
        parser.feed(decoded)
        parser.close()
        return parser.text()
    return _clean_document_text(decoded)


def _paragraphs(document_text: str) -> list[str]:
    return [value.strip() for value in document_text.split("\n\n") if value.strip()]


def _looks_like_boundary(value: str) -> bool:
    compact = " ".join(value.split()).strip()
    if not compact or len(compact) > 140:
        return False
    if _HEADING.match(compact) or _DATE_HEADING.match(compact):
        return True
    words = re.findall(r"\b[\w'-]+\b", compact)
    letters = [character for character in compact if character.isalpha()]
    uppercase_share = (
        sum(character.isupper() for character in letters) / len(letters)
        if letters
        else 0.0
    )
    return bool(words and len(words) <= 14 and uppercase_share >= 0.8)


def _seed_index(paragraphs: list[str], seed_text: str) -> int | None:
    normalized_seed = normalize_text(seed_text)
    if not normalized_seed:
        return None
    normalized = [normalize_text(value) for value in paragraphs]
    exact = [index for index, value in enumerate(normalized) if value == normalized_seed]
    if exact:
        return exact[0]
    contained = [
        (min(len(value), len(normalized_seed)), index)
        for index, value in enumerate(normalized)
        if value and (value in normalized_seed or normalized_seed in value)
    ]
    if contained:
        return max(contained)[1]
    seed_tokens = set(normalized_seed.split())
    scored = []
    for index, value in enumerate(normalized):
        tokens = set(value.split())
        if not tokens:
            continue
        score = len(seed_tokens & tokens) / max(1, min(len(seed_tokens), len(tokens)))
        scored.append((score, index))
    if not scored:
        return None
    score, index = max(scored)
    return index if score >= 0.35 else None


def _select_narrative(paragraphs: list[str], seed_index: int | None) -> dict:
    document_chars = sum(len(value) + 2 for value in paragraphs)
    if document_chars <= FULL_SOURCE_WHOLE_DOCUMENT_CHARS:
        selected = list(range(len(paragraphs)))
        return {
            "indices": selected,
            "boundary_before": "document_start",
            "boundary_after": "document_end",
            "selection_policy": "complete_visible_document",
        }
    if seed_index is None:
        return {
            "indices": [],
            "boundary_before": "seed_not_found",
            "boundary_after": "seed_not_found",
            "selection_policy": "unresolved_seed",
        }

    start = seed_index
    before_reason = "document_start"
    while start > 0:
        if _looks_like_boundary(paragraphs[start - 1]):
            before_reason = "structural_boundary"
            break
        start -= 1
    end = seed_index
    after_reason = "document_end"
    while end + 1 < len(paragraphs):
        if _looks_like_boundary(paragraphs[end + 1]):
            after_reason = "structural_boundary"
            break
        end += 1

    indices = list(range(start, end + 1))
    while len(indices) > FULL_SOURCE_MAX_NARRATIVE_PARAGRAPHS:
        if seed_index - indices[0] > indices[-1] - seed_index:
            indices.pop(0)
            before_reason = "paragraph_limit"
        else:
            indices.pop()
            after_reason = "paragraph_limit"
    while sum(len(paragraphs[index]) + 2 for index in indices) > FULL_SOURCE_MAX_NARRATIVE_CHARS:
        if len(indices) == 1:
            break
        if seed_index - indices[0] > indices[-1] - seed_index:
            indices.pop(0)
            before_reason = "character_limit"
        else:
            indices.pop()
            after_reason = "character_limit"
    return {
        "indices": indices,
        "boundary_before": before_reason,
        "boundary_after": after_reason,
        "selection_policy": "seed_centered_structural_unit",
    }


def assess_narrative_completeness(
    narrative_text: str,
    *,
    boundary_before: str,
    boundary_after: str,
) -> dict:
    compact = " ".join(narrative_text.split()).strip()
    lowered = compact.casefold()
    signals = []
    if any(marker in lowered for marker in _GATED_MARKERS):
        signals.append("access_or_preview_gate")
    first_alpha = next((character for character in compact if character.isalpha()), "")
    if first_alpha and first_alpha.islower() and boundary_before == "document_start":
        signals.append("leading_sentence_fragment")
    if compact and not compact.endswith(_CLOSING_PUNCTUATION):
        signals.append("trailing_sentence_fragment")
    if compact.endswith(("<", "-", "\u00ad")):
        signals.append("abrupt_terminal_character")
    if boundary_before in {"character_limit", "paragraph_limit", "seed_not_found"}:
        signals.append(f"before_{boundary_before}")
    if boundary_after in {"character_limit", "paragraph_limit", "seed_not_found"}:
        signals.append(f"after_{boundary_after}")

    gated = "access_or_preview_gate" in signals
    start_missing = any(
        value in signals
        for value in ("leading_sentence_fragment", "before_character_limit", "before_paragraph_limit")
    )
    end_missing = any(
        value in signals
        for value in (
            "trailing_sentence_fragment",
            "abrupt_terminal_character",
            "after_character_limit",
            "after_paragraph_limit",
        )
    )
    if gated:
        status = "gated_or_preview"
    elif start_missing and end_missing:
        status = "both_missing"
    elif start_missing:
        status = "start_missing"
    elif end_missing:
        status = "end_missing"
    elif "seed_not_found" in boundary_before + boundary_after:
        status = "unknown"
    else:
        status = "likely_complete"
    score = {
        "likely_complete": 1.0,
        "unknown": 0.5,
        "start_missing": 0.35,
        "end_missing": 0.35,
        "both_missing": 0.15,
        "gated_or_preview": 0.1,
    }[status]
    return {"status": status, "score": score, "signals": sorted(set(signals))}


def build_recovery_record(targets: list[dict], indexed: dict, fetched: dict) -> dict:
    payload = fetched["payload"]
    content_type = str(fetched.get("content_type", ""))
    document_text = extract_visible_document(payload, content_type)
    if not document_text:
        raise ValueError("indexed capture produced no visible document text")
    paragraphs = _paragraphs(document_text)
    units = []
    for target in targets:
        seed_text = str((target.get("seed") or {}).get("paragraph", ""))
        seed_index = _seed_index(paragraphs, seed_text)
        selection = _select_narrative(paragraphs, seed_index)
        if selection["indices"]:
            narrative = "\n\n".join(paragraphs[index] for index in selection["indices"])
        else:
            narrative = str((target.get("story") or {}).get("text", "")).strip()
        completeness = assess_narrative_completeness(
            narrative,
            boundary_before=selection["boundary_before"],
            boundary_after=selection["boundary_after"],
        )
        units.append(
            {
                "story_id": str(target["story_id"]),
                "record_id": str(target["record_id"]),
                "language": str(target.get("language", "unknown")),
                "seed_text": seed_text,
                "seed_text_sha256": _sha256_text(seed_text),
                "seed_document_paragraph_index": seed_index,
                "start_document_paragraph_index": (
                    selection["indices"][0] if selection["indices"] else None
                ),
                "end_document_paragraph_index": (
                    selection["indices"][-1] if selection["indices"] else None
                ),
                "selection_policy": selection["selection_policy"],
                "boundary_before": selection["boundary_before"],
                "boundary_after": selection["boundary_after"],
                "narrative_completeness": completeness,
                "narrative_text": narrative,
                "narrative_sha256": _sha256_text(narrative),
                "narrative_character_count": len(narrative),
            }
        )
    units.sort(key=lambda value: (value["story_id"], value["record_id"]))
    first = targets[0]
    capture_key = _capture_key(first)
    return {
        "schema_version": FULL_SOURCE_SCHEMA_VERSION,
        "recovery_version": FULL_SOURCE_RECOVERY_VERSION,
        "generated_text": False,
        "capture_key": capture_key,
        "capture_family_id": _sha256_text(normalize_url(str(first.get("url", ""))))[:24],
        "crawl_id": str(first.get("crawl_id", "")),
        "source_file": str(first.get("source_file", "")),
        "url": str(first.get("url", "")),
        "warc_date": str(first.get("warc_date", "")),
        "indexed_capture": {
            key: indexed.get(key)
            for key in ("url", "timestamp", "filename", "offset", "length", "digest", "mime")
            if indexed.get(key) is not None
        },
        "document": {
            "extraction_method": "deterministic_visible_text_from_indexed_warc",
            "content_type": content_type,
            "raw_payload_sha256": hashlib.sha256(payload).hexdigest(),
            "text": document_text,
            "text_sha256": _sha256_text(document_text),
            "character_count": len(document_text),
            "paragraph_count": len(paragraphs),
        },
        "narrative_units": units,
    }


def _recover_one(targets: list[dict], session: requests.Session | None = None) -> dict:
    session = session or _make_session()
    first = targets[0]
    indexed = lookup_index_capture(
        session,
        crawl_id=str(first["crawl_id"]),
        url=str(first["url"]),
        warc_date=str(first["warc_date"]),
    )
    fetched = fetch_indexed_payload(session, indexed)
    return build_recovery_record(targets, indexed, fetched)


def recover_full_sources(
    month: str,
    *,
    stories_dir: str | Path = STORIES_DIR,
    full_sources_dir: str | Path = FULL_SOURCES_DIR,
    export_root: str | Path | None = FULL_SOURCE_EXPORTS_DIR,
    limit: int | None = 10,
    workers: int = FULL_SOURCE_WORKERS,
    recoverer: Callable[[list[dict]], dict] | None = None,
    shutdown_event=None,
    heartbeat_callback: Callable[[], None] | None = None,
) -> dict:
    """Recover a bounded set of captures; completed records are skipped on resume."""
    if not 1 <= workers <= FULL_SOURCE_MAX_WORKERS:
        raise ValueError(f"workers must be between 1 and {FULL_SOURCE_MAX_WORKERS}")
    plan = plan_full_source_recovery(
        month,
        stories_dir=stories_dir,
        full_sources_dir=full_sources_dir,
        export_root=export_root,
        limit=limit,
    )
    groups = _target_groups(stories_dir, month)
    selected = [row["capture_key"] for row in plan["selection"]]
    operation = recoverer or _recover_one
    completed = []
    failures = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(operation, groups[key]): key for key in selected}
        for future in as_completed(futures):
            if shutdown_event is not None and shutdown_event.is_set():
                for pending_future in futures:
                    pending_future.cancel()
                break
            key = futures[future]
            try:
                record = future.result()
                if not _valid_recovery(record) or record["capture_key"] != key:
                    raise ValueError("recovery result failed deterministic validation")
                _write_gzip_json(_record_path(key, full_sources_dir), record)
                completed.append(
                    {
                        "capture_key": key,
                        "narrative_units": len(record["narrative_units"]),
                        "document_characters": record["document"]["character_count"],
                    }
                )
            except Exception as exc:  # recovery must leave failed work pending
                failures.append(
                    {"capture_key": key, "error_type": type(exc).__name__, "error": str(exc)}
                )
            if heartbeat_callback is not None:
                heartbeat_callback()
        if shutdown_event is not None and shutdown_event.is_set():
            for pending_future in futures:
                pending_future.cancel()
    return {
        **{key: value for key, value in plan.items() if key != "selection"},
        "completed_captures": len(completed),
        "failed_captures": len(failures),
        "interrupted": bool(shutdown_event is not None and shutdown_event.is_set()),
        "remaining_pending_captures": plan["pending_captures"] - len(completed),
        "completed": sorted(completed, key=lambda value: value["capture_key"]),
        "failures": sorted(failures, key=lambda value: value["capture_key"]),
    }


def _markdown_quote(value: str) -> str:
    return "\n".join(
        f"> {line.rstrip()}" if line.rstrip() else ">"
        for line in value.splitlines()
    )


def _stable_story_id(bounded_story_id: str) -> str:
    return "story-" + _sha256_text(
        f"{FULL_SOURCE_RECOVERY_VERSION}\x1f{bounded_story_id}"
    )[:24]


def export_full_sources(
    month: str,
    *,
    full_sources_dir: str | Path = FULL_SOURCES_DIR,
    export_root: str | Path = FULL_SOURCE_EXPORTS_DIR,
) -> dict:
    """Export recovered narratives without overwriting bounded story products."""
    records = [
        record
        for record in iter_full_source_records(full_sources_dir, export_root)
        if _capture_month(str(record.get("warc_date", ""))) == month
    ]
    output_dir = Path(export_root) / month
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for record in records:
        for unit in record["narrative_units"]:
            rows.append(
                {
                    "stable_story_id": _stable_story_id(str(unit["story_id"])),
                    "bounded_story_id": unit["story_id"],
                    "record_id": unit["record_id"],
                    "language": unit["language"],
                    "crawl_id": record["crawl_id"],
                    "source_file": record["source_file"],
                    "url": record["url"],
                    "warc_date": record["warc_date"],
                    "capture_key": record["capture_key"],
                    "capture_family_id": record["capture_family_id"],
                    "recovery_version": record["recovery_version"],
                    "indexed_capture": record.get("indexed_capture", {}),
                    "document": record["document"],
                    "narrative_unit": unit,
                }
            )
    rows.sort(
        key=lambda value: (
            value["warc_date"],
            value["language"],
            value["stable_story_id"],
            value["record_id"],
        )
    )
    structured_path = output_dir / "stories_full.jsonl.gz"
    with structured_path.open("wb") as raw:
        with gzip_binary_writer(raw) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as handle:
                for row in rows:
                    handle.write(
                        json.dumps(
                            row,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )

    by_language: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_language[row["language"]].append(row)
    generated = []
    for language, language_rows in sorted(by_language.items()):
        destination = output_dir / f"stories_{language}.md"
        temporary = destination.with_suffix(".md.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("# Full Recovered Home and Belonging Stories\n\n")
            handle.write(f"**Language:** `{language}`\n")
            handle.write(f"**Recovered Stories:** {len(language_rows)}\n")
            handle.write(f"**Capture Month:** `{month}`\n\n---\n\n")
            for position, row in enumerate(language_rows, 1):
                unit = row["narrative_unit"]
                completeness = unit["narrative_completeness"]
                handle.write(f"### Source Story for Match {position}\n")
                handle.write(f"- **Stable Story ID:** `{row['stable_story_id']}`\n")
                handle.write("- **Full Source Recovery:** `yes`\n")
                handle.write(f"- **Recovery Version:** `{row['recovery_version']}`\n")
                handle.write(
                    f"- **Narrative Completeness:** `{completeness['status']}`\n"
                )
                handle.write(
                    f"- **Completeness Score:** {float(completeness['score']):.2f}\n"
                )
                handle.write(
                    "- **Completeness Signals:** `"
                    + ", ".join(completeness["signals"])
                    + "`\n"
                )
                handle.write(
                    f"- **Narrative Characters:** {unit['narrative_character_count']}\n"
                )
                handle.write(
                    f"- **Captured Document Characters:** {row['document']['character_count']}\n"
                )
                handle.write(f"- **Capture Family:** `{row['capture_family_id']}`\n")
                handle.write(f"- **Original Bounded Story ID:** `{row['bounded_story_id']}`\n")
                handle.write(
                    "- **Extraction Method:** deterministic full-document recovery; "
                    "no generated or completed text\n"
                )
                handle.write(f"- **Source URL:** [{row['url']}]({row['url']})\n")
                handle.write(f"- **Crawl Dataset:** `{row['crawl_id']}`\n")
                handle.write(f"- **Source File:** `{row['source_file']}`\n\n")
                handle.write("#### Accepted Filter Paragraph\n\n")
                handle.write(_markdown_quote(str(unit["seed_text"])))
                handle.write("\n\n#### Extracted Source Story\n\n")
                handle.write(_markdown_quote(str(unit["narrative_text"])))
                handle.write("\n\n---\n" if position == len(language_rows) else "\n\n---\n\n")
        os.replace(temporary, destination)
        generated.append(str(destination))
    return {
        "schema_version": FULL_SOURCE_SCHEMA_VERSION,
        "recovery_version": FULL_SOURCE_RECOVERY_VERSION,
        "capture_month": month,
        "recovered_captures": len(records),
        "recovered_story_units": len(rows),
        "completeness": dict(
            sorted(
                (
                    status,
                    sum(
                        row["narrative_unit"]["narrative_completeness"]["status"]
                        == status
                        for row in rows
                    ),
                )
                for status in {
                    row["narrative_unit"]["narrative_completeness"]["status"]
                    for row in rows
                }
            )
        ),
        "structured_path": str(structured_path),
        "markdown_paths": generated,
    }
