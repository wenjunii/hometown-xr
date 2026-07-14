"""Real-corpus sampling, human annotation, and filter evaluation."""

from __future__ import annotations

import gzip
import hashlib
import heapq
import io
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Iterator

from config import (
    EVALUATION_DIR,
    EVALUATION_MAX_SAMPLES_PER_SESSION,
    EVALUATION_MIN_BASELINE_LABELS,
    EVALUATION_MIN_LANGUAGE_LABELS,
    EVALUATION_REPLAY_MAX_SAMPLES,
    EVALUATION_SAMPLE_RATE,
    EVALUATION_UNCERTAIN_SAMPLE_RATE,
    MIN_NARRATIVE_INDICATORS,
    OUTPUT_DIR,
    REPLAY_PATH,
    SEMANTIC_THRESHOLD,
)
from quality import classify_content
from record_identity import stable_record_id

if TYPE_CHECKING:
    from language_detector import LanguageDetector
    from matcher import MatchDecision


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    opener = gzip.open if path.suffix == ".gz" else Path.open
    with opener(path, "rt" if path.suffix == ".gz" else "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _atomic_gzip_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as handle:
                for row in rows:
                    handle.write(
                        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                    )
    os.replace(temporary, path)


def decision_uncertainty(
    semantic_score: float | None,
    narrative_score: int | None,
    semantic_threshold: float = SEMANTIC_THRESHOLD,
    narrative_threshold: int = MIN_NARRATIVE_INDICATORS,
) -> float:
    """Prioritize examples close to either model decision boundary."""
    semantic = 0.0
    narrative = 0.0
    if semantic_score is not None:
        semantic = max(0.0, 1.0 - abs(float(semantic_score) - semantic_threshold) / 0.08)
    if narrative_score is not None:
        narrative = max(0.0, 1.0 - abs(int(narrative_score) - narrative_threshold) / 4.0)
    return round(max(semantic, narrative), 4)


def _enrich_for_active_learning(row: dict) -> dict:
    enriched = dict(row)
    uncertainty = decision_uncertainty(
        enriched.get("semantic_score"),
        enriched.get("narrative_score"),
    )
    enriched["schema_version"] = max(2, int(enriched.get("schema_version", 1)))
    enriched["uncertainty_score"] = uncertainty
    enriched.setdefault(
        "selection_reason",
        "decision_boundary" if uncertainty >= 0.5 else "coverage",
    )
    return enriched


class DecisionSampler:
    """Persist a deterministic, bounded sample of live filter decisions."""

    def __init__(
        self,
        path: str | Path = EVALUATION_DIR / "candidate_samples.jsonl",
        sample_rate: float = EVALUATION_SAMPLE_RATE,
        max_samples: int = EVALUATION_MAX_SAMPLES_PER_SESSION,
    ):
        self.path = Path(path)
        self.sample_rate = sample_rate
        self.max_samples = max_samples
        self.written = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._known = {row.get("sample_id") for row in _read_jsonl(self.path)}

    def observe(
        self,
        decisions: list[MatchDecision],
        language_detector: LanguageDetector,
    ) -> int:
        if self.written >= self.max_samples:
            return 0
        selected = []
        for decision in decisions:
            paragraph = decision.paragraph
            sample_id = stable_record_id(
                paragraph.crawl_id,
                paragraph.source_file,
                paragraph.url,
                paragraph.warc_date,
                paragraph.text,
            )
            rank = int(sample_id[:16], 16) / float(2**64)
            uncertainty = decision_uncertainty(
                decision.semantic_score,
                decision.narrative_score,
            )
            uncertain_rank = int(sample_id[16:32], 16) / float(2**64)
            selected_for_coverage = rank < self.sample_rate
            selected_for_uncertainty = (
                uncertainty >= 0.5 and uncertain_rank < EVALUATION_UNCERTAIN_SAMPLE_RATE
            )
            if (
                not selected_for_coverage
                and not selected_for_uncertainty
                or sample_id in self._known
            ):
                continue
            language, confidence = language_detector.detect(paragraph.text)
            selected.append(
                {
                    "schema_version": 3,
                    "sample_id": sample_id,
                    "collected_at": _utc_now(),
                    "crawl_id": paragraph.crawl_id,
                    "source_file": paragraph.source_file,
                    "url": paragraph.url,
                    "warc_date": paragraph.warc_date,
                    "language": language,
                    "language_confidence": round(confidence, 4),
                    "paragraph": paragraph.text,
                    "document_id": paragraph.document_id,
                    "paragraph_index": paragraph.paragraph_index,
                    "context_before": paragraph.context_before,
                    "context_after": paragraph.context_after,
                    "matched_keywords": decision.matched_keywords,
                    "semantic_score": round(decision.semantic_score, 6),
                    "concept_match": decision.concept_match,
                    "narrative_score": decision.narrative_score,
                    "predicted_accept": decision.accepted,
                    "rejection_reason": decision.rejection_reason,
                    "uncertainty_score": uncertainty,
                    "selection_reason": (
                        "decision_boundary"
                        if selected_for_uncertainty
                        else "coverage"
                    ),
                    "predicted_content_category": classify_content(
                        paragraph.text, paragraph.url
                    ).category,
                }
            )
            self._known.add(sample_id)
            if self.written + len(selected) >= self.max_samples:
                break

        if selected:
            with self.path.open("a", encoding="utf-8") as handle:
                for row in selected:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.written += len(selected)
        return len(selected)


def iter_output_records(output_dir: str | Path = OUTPUT_DIR) -> Iterator[dict]:
    root = Path(output_dir)
    if not root.exists():
        return
    for language_dir in sorted(root.iterdir()):
        if not language_dir.is_dir() or language_dir.name.startswith((".", "_")):
            continue
        for path in sorted(language_dir.glob("*.jsonl.gz")):
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        yield json.loads(line)


def _rank(row: dict) -> int:
    sample_id = str(row.get("sample_id") or row.get("record_id") or row.get("paragraph", ""))
    return int(hashlib.sha256(sample_id.encode("utf-8")).hexdigest()[:16], 16)


def _active_learning_pick(rows: Iterable[dict], limit: int) -> list[dict]:
    """Round-robin languages while taking decision-boundary cases first."""
    if limit <= 0:
        return []
    buckets: dict[str, list[tuple[float, int, int, dict]]] = defaultdict(list)
    for sequence, source_row in enumerate(rows):
        row = _enrich_for_active_learning(source_row)
        language = str(row.get("language") or "unknown")
        item = (
            float(row.get("uncertainty_score", 0.0)),
            -_rank(row),
            sequence,
            row,
        )
        if len(buckets[language]) < limit:
            heapq.heappush(buckets[language], item)
        elif item > buckets[language][0]:
            heapq.heapreplace(buckets[language], item)

    ordered = {
        language: [
            item[3]
            for item in sorted(
                items,
                key=lambda item: (-item[0], -item[1], item[2]),
            )
        ]
        for language, items in buckets.items()
    }

    selected = []
    while len(selected) < limit:
        added = False
        for language in sorted(ordered):
            if ordered[language]:
                selected.append(ordered[language].pop(0))
                added = True
                if len(selected) == limit:
                    break
        if not added:
            break
    return selected


def compact_replay_reservoir(
    candidate_path: str | Path = EVALUATION_DIR / "candidate_samples.jsonl",
    replay_path: str | Path = REPLAY_PATH,
    max_samples: int = EVALUATION_REPLAY_MAX_SAMPLES,
    clear_local: bool = True,
) -> dict:
    """Merge local decisions into a deterministic cross-workstation reservoir."""
    if max_samples <= 0:
        raise ValueError("max_samples must be positive")
    candidate_path = Path(candidate_path)
    replay_path = Path(replay_path)
    merged: dict[str, dict] = {}
    for row in [*_read_jsonl(replay_path), *_read_jsonl(candidate_path)]:
        sample_id = str(row.get("sample_id", ""))
        if sample_id:
            merged[sample_id] = _enrich_for_active_learning(row)

    buckets: dict[tuple[bool, str], list[dict]] = defaultdict(list)
    for row in merged.values():
        key = (bool(row.get("predicted_accept")), str(row.get("language", "unknown")))
        buckets[key].append(row)
    for rows in buckets.values():
        rows.sort(
            key=lambda row: (
                -float(row.get("uncertainty_score", 0.0)),
                _rank(row),
            )
        )

    selected: list[dict] = []
    while len(selected) < max_samples:
        added = False
        for key in sorted(buckets, key=lambda value: (value[0], value[1])):
            if buckets[key]:
                selected.append(buckets[key].pop(0))
                added = True
                if len(selected) >= max_samples:
                    break
        if not added:
            break
    selected.sort(key=lambda row: str(row.get("sample_id", "")))
    before = replay_path.stat().st_size if replay_path.exists() else 0
    if selected:
        _atomic_gzip_jsonl(replay_path, selected)
    if clear_local and candidate_path.exists():
        candidate_path.unlink()
    return {
        "samples": len(selected),
        "accepted": sum(bool(row.get("predicted_accept")) for row in selected),
        "rejected": sum(not bool(row.get("predicted_accept")) for row in selected),
        "languages": len({str(row.get("language", "unknown")) for row in selected}),
        "bytes_before": before,
        "bytes_after": replay_path.stat().st_size if replay_path.exists() else 0,
        "path": str(replay_path),
    }


def _output_annotation_rows(output_dir: str | Path) -> Iterator[dict]:
    for record in iter_output_records(output_dir):
        source_file = record.get("source_file", "")
        sample_id = record.get("record_id") or stable_record_id(
            record.get("crawl_id", ""),
            source_file,
            record.get("url", ""),
            record.get("warc_date", ""),
            record.get("paragraph", ""),
        )
        yield _enrich_for_active_learning({
            "schema_version": 3,
            "sample_id": sample_id,
            "sample_origin": "committed_output",
            "crawl_id": record.get("crawl_id", ""),
            "source_file": source_file,
            "url": record.get("url", ""),
            "warc_date": record.get("warc_date", ""),
            "language": record.get("language", "unknown"),
            "language_confidence": record.get("language_confidence", 0.0),
            "paragraph": record.get("paragraph", ""),
            "matched_keywords": record.get("matched_keywords", []),
            "semantic_score": record.get("semantic_score"),
            "concept_match": record.get("concept_match", ""),
            "narrative_score": record.get("narrative_score", MIN_NARRATIVE_INDICATORS),
            "predicted_accept": True,
            "rejection_reason": None,
            "label": None,
            "notes": "",
            "predicted_content_category": classify_content(
                str(record.get("paragraph", "")), str(record.get("url", ""))
            ).category,
        })


def build_annotation_sample(
    size: int = 400,
    output_dir: str | Path = OUTPUT_DIR,
    candidate_path: str | Path = EVALUATION_DIR / "candidate_samples.jsonl",
    annotation_path: str | Path = EVALUATION_DIR / "annotations.jsonl",
    replay_path: str | Path = REPLAY_PATH,
) -> dict:
    """Create a balanced, language-stratified sample of real project text."""
    if size <= 0:
        raise ValueError("sample size must be positive")
    annotation_path = Path(annotation_path)
    candidate_path = Path(candidate_path)
    existing = {row["sample_id"]: row for row in _read_jsonl(annotation_path)}

    positive_target = size // 2
    output_rows = _active_learning_pick(_output_annotation_rows(output_dir), positive_target)
    candidate_rows_by_id = {
        str(row.get("sample_id", "")): row
        for row in [*_read_jsonl(Path(replay_path)), *_read_jsonl(candidate_path)]
        if row.get("sample_id")
    }
    candidate_rows = list(candidate_rows_by_id.values())
    rejected = [
        _enrich_for_active_learning({
            **row,
            "sample_origin": "live_candidate",
            "label": None,
            "notes": "",
        })
        for row in candidate_rows
        if not row.get("predicted_accept", False)
    ]
    rejected_rows = _active_learning_pick(rejected, size - len(output_rows))

    rows = output_rows + rejected_rows
    if len(rows) < size:
        selected_ids = {row["sample_id"] for row in rows}
        extras = [
            _enrich_for_active_learning({
                **row,
                "sample_origin": "live_candidate",
                "label": None,
                "notes": "",
            })
            for row in candidate_rows
            if row.get("sample_id") not in selected_ids
        ]
        rows.extend(_active_learning_pick(extras, size - len(rows)))
    if len(rows) < size:
        selected_ids = {row["sample_id"] for row in rows}
        extra_output = (
            row
            for row in _output_annotation_rows(output_dir)
            if row["sample_id"] not in selected_ids
        )
        rows.extend(_active_learning_pick(extra_output, size - len(rows)))

    for row in rows:
        old = existing.get(row["sample_id"])
        if old:
            row["label"] = old.get("label")
            row["notes"] = old.get("notes", "")
            row["content_label"] = old.get("content_label")
        row.update(
            {
                key: value
                for key, value in _enrich_for_active_learning(row).items()
                if key in {"schema_version", "uncertainty_score", "selection_reason"}
            }
        )
    selected_ids = {row["sample_id"] for row in rows}
    for old in existing.values():
        if old.get("label") not in {"positive", "negative"}:
            continue
        if old["sample_id"] not in selected_ids:
            rows.append(_enrich_for_active_learning(old))
            selected_ids.add(old["sample_id"])
    while len(rows) > size:
        removable = next(
            (
                index
                for index in range(len(rows) - 1, -1, -1)
                if rows[index].get("label") not in {"positive", "negative"}
            ),
            None,
        )
        if removable is None:
            break
        rows.pop(removable)
    rows.sort(key=lambda row: (str(row.get("language", "")), _rank(row)))
    _atomic_jsonl(annotation_path, rows)
    return {
        "path": str(annotation_path),
        "samples": len(rows),
        "predicted_positive": sum(bool(row.get("predicted_accept")) for row in rows),
        "predicted_negative": sum(not bool(row.get("predicted_accept")) for row in rows),
        "labeled": sum(row.get("label") in {"positive", "negative"} for row in rows),
        "uncertain": sum(float(row.get("uncertainty_score", 0.0)) >= 0.5 for row in rows),
    }


def annotate(
    annotation_path: str | Path = EVALUATION_DIR / "annotations.jsonl",
    language: str | None = None,
    limit: int | None = None,
) -> dict:
    path = Path(annotation_path)
    rows = _read_jsonl(path)
    if not rows:
        raise FileNotFoundError(f"No annotation sample at {path}; run evaluation sample first")

    labeled_now = 0
    for index, row in enumerate(rows, start=1):
        if row.get("label") in {"positive", "negative"}:
            continue
        if language and row.get("language") != language:
            continue
        if limit is not None and labeled_now >= limit:
            break
        print("\n" + "=" * 78)
        print(f"Sample {index}/{len(rows)} | language={row.get('language', 'unknown')}")
        print(
            f"Model={'ACCEPT' if row.get('predicted_accept') else 'REJECT'} | "
            f"semantic={row.get('semantic_score')} | narrative={row.get('narrative_score')} | "
            f"uncertainty={row.get('uncertainty_score', 0)}"
        )
        print(f"URL: {row.get('url', '')}")
        print(f"Selection: {row.get('selection_reason', 'coverage')}")
        print(
            f"Content type: {row.get('predicted_content_category', 'unknown')}"
        )
        print("-" * 78)
        print(row.get("paragraph", ""))
        answer = input("\n[p]ositive [n]egative [t]note [s]kip [q]uit: ").strip().lower()
        if answer == "q":
            break
        if answer == "p":
            row["label"] = "positive"
        elif answer == "n":
            row["label"] = "negative"
        elif answer == "t":
            row["notes"] = input("Note: ").strip()
            _atomic_jsonl(path, rows)
            continue
        else:
            continue
        category_codes = {
            "p": "personal_prose",
            "l": "lyrics",
            "o": "poetry",
            "c": "commercial",
            "g": "genealogy",
            "a": "adult_content",
            "u": "unknown",
        }
        category = input(
            "Content [p]ersonal [l]yrics p[o]etry [c]ommercial "
            "[g]enealogy [a]dult [u]nknown [Enter=model]: "
        ).strip().lower()
        row["content_label"] = category_codes.get(
            category,
            row.get("predicted_content_category", "unknown"),
        )
        labeled_now += 1
        _atomic_jsonl(path, rows)
    return {
        "path": str(path),
        "labeled_now": labeled_now,
        "labeled_total": sum(
            row.get("label") in {"positive", "negative"} for row in rows
        ),
        "remaining": sum(
            row.get("label") not in {"positive", "negative"} for row in rows
        ),
    }


def _wilson(successes: int, total: int) -> list[float] | None:
    if total <= 0:
        return None
    z = 1.959963984540054
    estimate = successes / total
    denominator = 1 + z**2 / total
    center = (estimate + z**2 / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(estimate * (1 - estimate) / total + z**2 / (4 * total**2))
        / denominator
    )
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


def _classification(
    rows: list[dict],
    semantic: float | None = None,
    narrative: int | None = None,
) -> dict:
    confusion = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    for row in rows:
        actual = row["label"] == "positive"
        if semantic is None or narrative is None:
            predicted = bool(row.get("predicted_accept"))
        else:
            score = row.get("semantic_score")
            narrative_score = row.get("narrative_score")
            if score is None or narrative_score is None:
                continue
            predicted = float(score) >= semantic and int(narrative_score) >= narrative
        if predicted and actual:
            confusion["tp"] += 1
        elif predicted:
            confusion["fp"] += 1
        elif actual:
            confusion["fn"] += 1
        else:
            confusion["tn"] += 1

    tp, fp, fn = confusion["tp"], confusion["fp"], confusion["fn"]
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        **confusion,
        "precision": round(precision, 4),
        "precision_ci95": _wilson(tp, tp + fp),
        "recall": round(recall, 4),
        "recall_ci95": _wilson(tp, tp + fn),
        "f1": round(f1, 4),
    }


def _best_threshold(rows: list[dict]) -> dict:
    best = None
    for semantic_step in range(30, 71):
        semantic = semantic_step / 100
        for narrative in range(0, 17):
            result = _classification(rows, semantic, narrative)
            candidate = {
                "semantic_threshold": semantic,
                "narrative_threshold": narrative,
                **result,
            }
            if best is None or (
                candidate["f1"],
                candidate["precision"],
                candidate["recall"],
            ) > (best["f1"], best["precision"], best["recall"]):
                best = candidate
    return best


def _calibration(rows: list[dict]) -> list[dict]:
    bins: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        score = row.get("semantic_score")
        if score is not None:
            bins[min(9, max(0, int(float(score) * 10)))].append(row)
    result = []
    for index in sorted(bins):
        values = bins[index]
        positives = sum(row["label"] == "positive" for row in values)
        result.append(
            {
                "score_range": [round(index / 10, 1), round((index + 1) / 10, 1)],
                "samples": len(values),
                "observed_positive_rate": round(positives / len(values), 4),
                "ci95": _wilson(positives, len(values)),
            }
        )
    return result


def evaluation_report(
    annotation_path: str | Path = EVALUATION_DIR / "annotations.jsonl",
    report_path: str | Path = EVALUATION_DIR / "report.json",
) -> dict:
    all_rows = _read_jsonl(Path(annotation_path))
    rows = [
        row
        for row in all_rows
        if row.get("label") in {"positive", "negative"}
    ]
    if not rows:
        raise ValueError("No human-labeled rows are available")

    positives = sum(row["label"] == "positive" for row in rows)
    negatives = len(rows) - positives
    baseline_ready = (
        len(rows) >= EVALUATION_MIN_BASELINE_LABELS and positives > 0 and negatives > 0
    )
    by_language = {}
    for language in sorted({str(row.get("language", "unknown")) for row in rows}):
        language_rows = [row for row in rows if str(row.get("language", "unknown")) == language]
        language_positives = sum(row["label"] == "positive" for row in language_rows)
        language_ready = (
            len(language_rows) >= EVALUATION_MIN_LANGUAGE_LABELS
            and 0 < language_positives < len(language_rows)
        )
        by_language[language] = {
            "samples": len(language_rows),
            "ready_for_calibration": language_ready,
            "minimum_samples": EVALUATION_MIN_LANGUAGE_LABELS,
            **_classification(language_rows),
            "recommended_thresholds": (
                _best_threshold(language_rows) if language_ready else None
            ),
        }

    best = _best_threshold(rows)
    human_categories = Counter(
        str(row["content_label"])
        for row in rows
        if row.get("content_label")
    )
    predicted_categories = Counter(
        str(row.get("predicted_content_category", "unknown")) for row in rows
    )
    categorized = [row for row in rows if row.get("content_label")]
    category_agreement = (
        sum(
            row.get("content_label") == row.get("predicted_content_category")
            for row in categorized
        )
        / len(categorized)
        if categorized
        else None
    )

    payload = {
        "schema_version": 2,
        "generated_at": _utc_now(),
        "human_labeled_samples": len(rows),
        "unlabeled_samples": sum(
            row.get("label") not in {"positive", "negative"} for row in all_rows
        ),
        "label_balance": {"positive": positives, "negative": negatives},
        "baseline": {
            "ready": baseline_ready,
            "minimum_labels": EVALUATION_MIN_BASELINE_LABELS,
            "requires_both_classes": True,
            "warning": (
                None
                if baseline_ready
                else "Threshold recommendations remain exploratory until the baseline is ready."
            ),
        },
        "overall": _classification(rows),
        "by_language": by_language,
        "semantic_calibration": _calibration(rows),
        "content_taxonomy": {
            "human_labeled": dict(sorted(human_categories.items())),
            "predicted": dict(sorted(predicted_categories.items())),
            "agreement": round(category_agreement, 4)
            if category_agreement is not None
            else None,
        },
        "recommended_thresholds": {**best, "exploratory": not baseline_ready},
        "false_positives": [
            row["sample_id"]
            for row in rows
            if row.get("predicted_accept") and row["label"] == "negative"
        ],
        "false_negatives": [
            row["sample_id"]
            for row in rows
            if not row.get("predicted_accept") and row["label"] == "positive"
        ],
    }
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, report_path)
    return payload
