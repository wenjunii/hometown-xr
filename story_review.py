"""Search, review, and curate deterministic story exports."""

from __future__ import annotations

import gzip
import io
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from config import DATA_DIR, STORIES_DIR
from deterministic_gzip import gzip_binary_writer

STORY_REVIEW_SCHEMA_VERSION = 1
STORY_REVIEW_DECISIONS = {"selected", "rejected", "unsure"}
DEFAULT_STORY_EXPORT = DATA_DIR / "exports" / "stories_complete.jsonl.gz"
DEFAULT_STORY_REVIEWS = STORIES_DIR / "reviews.jsonl"
DEFAULT_REVIEW_EXPORT = DATA_DIR / "exports" / "stories_reviewed.jsonl.gz"
_REVIEW_LOCK = threading.RLock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
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
    os.replace(temporary, path)


def _atomic_gzip_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as raw:
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
    os.replace(temporary, path)


def load_story_export(path: str | Path = DEFAULT_STORY_EXPORT) -> list[dict]:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(
            f"Story export is missing: {source}. Run stories export first."
        )
    with gzip.open(source, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return sorted(
        rows,
        key=lambda row: (
            min(row.get("match_numbers") or [10**12]),
            str(row.get("story_id", "")),
        ),
    )


def load_story_reviews(path: str | Path = DEFAULT_STORY_REVIEWS) -> dict[str, dict]:
    source = Path(path)
    if not source.exists():
        return {}
    rows = {}
    with source.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            story_id = str(row.get("story_id", ""))
            if story_id:
                rows[story_id] = row
    return rows


def save_story_review(
    story_id: str,
    decision: str,
    *,
    notes: str = "",
    reviewer: str = "",
    path: str | Path = DEFAULT_STORY_REVIEWS,
    known_story_ids: set[str] | None = None,
) -> dict:
    story_id = story_id.strip()
    decision = decision.strip().lower()
    if not story_id:
        raise ValueError("story_id is required")
    if known_story_ids is not None and story_id not in known_story_ids:
        raise ValueError("story_id is not present in the current story export")
    if decision not in STORY_REVIEW_DECISIONS and decision != "unreviewed":
        raise ValueError(
            "decision must be selected, rejected, unsure, or unreviewed"
        )
    target = Path(path)
    with _REVIEW_LOCK:
        reviews = load_story_reviews(target)
        if decision == "unreviewed":
            removed = reviews.pop(story_id, None)
            _atomic_jsonl(
                target,
                [reviews[key] for key in sorted(reviews)],
            )
            return {
                "story_id": story_id,
                "decision": "unreviewed",
                "removed": removed is not None,
                "reviewed_stories": len(reviews),
            }
        row = {
            "schema_version": STORY_REVIEW_SCHEMA_VERSION,
            "story_id": story_id,
            "decision": decision,
            "notes": notes.strip(),
            "reviewer": reviewer.strip(),
            "reviewed_at": _utc_now(),
        }
        reviews[story_id] = row
        _atomic_jsonl(target, [reviews[key] for key in sorted(reviews)])
        return {**row, "reviewed_stories": len(reviews)}


def _domain(row: dict) -> str:
    return urlparse(str(row.get("url", ""))).netloc.lower()


def _story_text(row: dict) -> str:
    story = row.get("story") or {}
    if story.get("text"):
        return str(story["text"])
    return "\n\n".join(
        str(paragraph.get("text", ""))
        for paragraph in story.get("paragraphs", [])
        if paragraph.get("text")
    )


def _summary(row: dict, review: dict | None) -> dict:
    seed = row.get("seed") or {}
    story = row.get("story") or {}
    text = _story_text(row)
    return {
        "story_id": str(row.get("story_id", "")),
        "record_id": str(row.get("record_id", "")),
        "match_numbers": list(row.get("match_numbers") or []),
        "language": str(row.get("language", "unknown")),
        "domain": _domain(row),
        "url": str(row.get("url", "")),
        "warc_date": str(row.get("warc_date", "")),
        "capture_count": int(row.get("capture_count", 1)),
        "semantic_score": float(seed.get("semantic_score", 0.0)),
        "narrative_score": int(seed.get("narrative_score", 0) or 0),
        "matched_keywords": list(seed.get("matched_keywords") or []),
        "concept_match": str(seed.get("concept_match", "")),
        "seed_paragraph": str(seed.get("paragraph", "")),
        "character_count": int(story.get("character_count", len(text)) or 0),
        "paragraph_count": int(
            story.get("paragraph_count", len(story.get("paragraphs", []))) or 0
        ),
        "excerpt": " ".join(text.split())[:320],
        "decision": str((review or {}).get("decision", "unreviewed")),
        "notes": str((review or {}).get("notes", "")),
        "reviewer": str((review or {}).get("reviewer", "")),
        "reviewed_at": (review or {}).get("reviewed_at"),
    }


class StoryReviewIndex:
    """In-memory searchable view over the deterministic story export."""

    def __init__(
        self,
        export_path: str | Path = DEFAULT_STORY_EXPORT,
        reviews_path: str | Path = DEFAULT_STORY_REVIEWS,
    ):
        self.export_path = Path(export_path)
        self.reviews_path = Path(reviews_path)
        self.rows = load_story_export(self.export_path)
        self.by_id = {
            str(row["story_id"]): row for row in self.rows if row.get("story_id")
        }
        self._search = {
            story_id: " ".join(
                (
                    str(row.get("language", "")),
                    _domain(row),
                    str((row.get("seed") or {}).get("paragraph", "")),
                    str((row.get("seed") or {}).get("concept_match", "")),
                    " ".join((row.get("seed") or {}).get("matched_keywords") or []),
                    _story_text(row),
                )
            ).casefold()
            for story_id, row in self.by_id.items()
        }

    def reviews(self) -> dict[str, dict]:
        return load_story_reviews(self.reviews_path)

    def status(self) -> dict:
        reviews = self.reviews()
        decisions = {decision: 0 for decision in STORY_REVIEW_DECISIONS}
        for review in reviews.values():
            decision = str(review.get("decision", ""))
            if decision in decisions:
                decisions[decision] += 1
        domains = sorted({_domain(row) for row in self.rows if _domain(row)})
        languages = sorted(
            {str(row.get("language", "unknown")) for row in self.rows}
        )
        keywords = sorted(
            {
                str(keyword)
                for row in self.rows
                for keyword in (row.get("seed") or {}).get(
                    "matched_keywords", []
                )
            }
        )
        return {
            "schema_version": STORY_REVIEW_SCHEMA_VERSION,
            "stories": len(self.rows),
            "reviewed": len(reviews),
            "unreviewed": len(self.rows) - len(reviews),
            "decisions": decisions,
            "languages": languages,
            "domains": domains,
            "keywords": keywords,
            "deterministic_extraction": True,
            "generated_text": False,
        }

    def query(
        self,
        *,
        search: str = "",
        language: str = "",
        domain: str = "",
        keyword: str = "",
        decision: str = "",
        sort: str = "match",
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        if offset < 0:
            raise ValueError("offset cannot be negative")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if decision and decision not in {
            *STORY_REVIEW_DECISIONS,
            "unreviewed",
        }:
            raise ValueError("invalid review decision")
        reviews = self.reviews()
        needle = search.strip().casefold()
        rows = []
        for row in self.rows:
            story_id = str(row.get("story_id", ""))
            review = reviews.get(story_id)
            row_decision = str((review or {}).get("decision", "unreviewed"))
            if needle and needle not in self._search.get(story_id, ""):
                continue
            if language and str(row.get("language", "")) != language:
                continue
            if domain and _domain(row) != domain:
                continue
            if keyword and keyword not in (
                (row.get("seed") or {}).get("matched_keywords") or []
            ):
                continue
            if decision and row_decision != decision:
                continue
            rows.append(_summary(row, review))
        if sort == "score":
            rows.sort(
                key=lambda row: (
                    -row["semantic_score"],
                    row["match_numbers"] or [10**12],
                    row["story_id"],
                )
            )
        elif sort == "length":
            rows.sort(
                key=lambda row: (
                    -row["character_count"],
                    row["match_numbers"] or [10**12],
                    row["story_id"],
                )
            )
        elif sort == "review":
            rows.sort(
                key=lambda row: (
                    row["decision"] == "unreviewed",
                    row["decision"],
                    row["match_numbers"] or [10**12],
                )
            )
        elif sort != "match":
            raise ValueError("sort must be match, score, length, or review")
        total = len(rows)
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "stories": rows[offset : offset + limit],
        }

    def detail(self, story_id: str) -> dict:
        row = self.by_id.get(story_id)
        if row is None:
            raise KeyError("story not found")
        review = self.reviews().get(story_id)
        return {
            **row,
            "domain": _domain(row),
            "review": review,
            "deterministic_extraction": True,
            "generated_text": False,
        }

    def review(
        self,
        story_id: str,
        decision: str,
        *,
        notes: str = "",
        reviewer: str = "",
    ) -> dict:
        return save_story_review(
            story_id,
            decision,
            notes=notes,
            reviewer=reviewer,
            path=self.reviews_path,
            known_story_ids=set(self.by_id),
        )


def export_reviewed_stories(
    export_path: str | Path = DEFAULT_STORY_EXPORT,
    reviews_path: str | Path = DEFAULT_STORY_REVIEWS,
    output_path: str | Path = DEFAULT_REVIEW_EXPORT,
) -> dict:
    """Export selected stories without changing or generating source text."""
    stories = {
        str(row["story_id"]): row
        for row in load_story_export(export_path)
        if row.get("story_id")
    }
    reviews = load_story_reviews(reviews_path)
    selected = []
    for story_id in sorted(
        (
            story_id
            for story_id, review in reviews.items()
            if review.get("decision") == "selected" and story_id in stories
        ),
        key=lambda story_id: (
            min(stories[story_id].get("match_numbers") or [10**12]),
            story_id,
        ),
    ):
        selected.append(
            {
                **stories[story_id],
                "review": reviews[story_id],
            }
        )
    target = Path(output_path)
    _atomic_gzip_jsonl(target, selected)
    markdown_path = target.with_suffix("").with_suffix(".md")
    lines = [
        "# Reviewed Source Stories",
        "",
        (
            "Deterministic source extraction with human selection metadata; "
            "no generated or rewritten story text."
        ),
        "",
    ]
    for index, row in enumerate(selected, 1):
        seed = row.get("seed") or {}
        review = row["review"]
        match_numbers = ", ".join(
            str(number) for number in row.get("match_numbers") or []
        )
        lines.extend(
            [
                f"## {index}. Matches {match_numbers}",
                "",
                f"- **Language:** `{row.get('language', 'unknown')}`",
                f"- **Source:** {row.get('url', '')}",
                (
                    "- **Keywords:** "
                    + ", ".join(
                        f"`{keyword}`"
                        for keyword in seed.get("matched_keywords") or []
                    )
                ),
                f"- **Review Notes:** {review.get('notes', '')}",
                "",
                str((row.get("story") or {}).get("text", "")).strip(),
                "",
            ]
        )
    markdown_path.write_text(
        "\n".join(lines).rstrip() + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "schema_version": STORY_REVIEW_SCHEMA_VERSION,
        "selected_stories": len(selected),
        "structured_path": str(target),
        "markdown_path": str(markdown_path),
        "deterministic_extraction": True,
        "generated_text": False,
    }
