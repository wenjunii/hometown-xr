"""Real-corpus sampling, human annotation, and filter evaluation."""

from __future__ import annotations

import gzip
import hashlib
import heapq
import io
import json
import math
import os
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Iterator

from config import (
    EVALUATION_DIR,
    EVALUATION_HOLDOUT_RATE,
    EVALUATION_MAX_SAMPLES_PER_SESSION,
    EVALUATION_MIN_BASELINE_LABELS,
    EVALUATION_MIN_HOLDOUT_LABELS,
    EVALUATION_MIN_LANGUAGE_LABELS,
    EVALUATION_MINORITY_LANGUAGE_QUOTA,
    EVALUATION_MINORITY_PROBE_RATE,
    EVALUATION_REPLAY_MAX_SAMPLES,
    EVALUATION_SAMPLE_RATE,
    EVALUATION_UNCERTAIN_SAMPLE_RATE,
    MIN_NARRATIVE_INDICATORS,
    OUTPUT_DIR,
    OUTPUT_SCHEMA_VERSION,
    REPLAY_PATH,
    SEMANTIC_THRESHOLD,
)
from deterministic_gzip import gzip_binary_writer
from quality import classify_content
from record_identity import stable_record_id

if TYPE_CHECKING:
    from language_detector import LanguageDetector
    from matcher import MatchDecision

_ANNOTATION_LOCK = threading.RLock()


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
        with gzip_binary_writer(raw) as compressed:
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


def _evaluation_split(sample_id: str, role: str) -> str:
    """Keep representative holdout rows stable across PCs and sample rebuilds."""
    if role != "benchmark":
        return "tuning"
    rank = int(hashlib.sha256(f"holdout\0{sample_id}".encode("utf-8")).hexdigest()[:16], 16)
    return "holdout" if rank / float(2**64) < EVALUATION_HOLDOUT_RATE else "tuning"


def _sampling_stratum(row: dict) -> str:
    if row.get("sampling_stratum"):
        return str(row["sampling_stratum"])
    if row.get("rejection_reason") == "keyword_prefilter":
        return "keyword_reject"
    if row.get("predicted_accept"):
        return "filter_accept"
    return "filter_reject"


def _sample_weight(row: dict) -> float | None:
    probability = row.get("sampling_probability")
    try:
        value = float(probability)
    except (TypeError, ValueError):
        return None
    if not 0 < value <= 1:
        return None
    return 1.0 / value


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
    enriched.setdefault("sampling_stratum", _sampling_stratum(enriched))
    enriched.setdefault("sample_role", "legacy")
    enriched.setdefault(
        "evaluation_split",
        _evaluation_split(
            str(enriched.get("sample_id", "")),
            str(enriched.get("sample_role", "legacy")),
        ),
    )
    enriched.setdefault("sampling_probability", None)
    enriched["sample_weight"] = _sample_weight(enriched)
    return enriched


class DecisionSampler:
    """Persist a deterministic, bounded sample of live filter decisions."""

    def __init__(
        self,
        path: str | Path = EVALUATION_DIR / "candidate_samples.jsonl",
        sample_rate: float = EVALUATION_SAMPLE_RATE,
        max_samples: int = EVALUATION_MAX_SAMPLES_PER_SESSION,
        replay_path: str | Path = REPLAY_PATH,
        representative: bool = True,
    ):
        if not 0 <= sample_rate <= 1:
            raise ValueError("sample_rate must be between 0 and 1")
        if max_samples < 0:
            raise ValueError("max_samples cannot be negative")
        self.path = Path(path)
        self.sample_rate = sample_rate
        self.max_samples = max_samples
        self.representative = representative
        self.written = 0
        self.tuning_written = 0
        self.benchmark_written = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        known_rows = [*_read_jsonl(Path(replay_path)), *_read_jsonl(self.path)]
        self._known_roles = {
            str(row.get("sample_id")): str(row.get("sample_role", "legacy"))
            for row in known_rows
            if row.get("sample_id")
        }
        self._known = set(self._known_roles)
        self._language_written = Counter(
            str(row.get("language") or "unknown") for row in known_rows
        )

    def observe(
        self,
        decisions: list[MatchDecision],
        language_detector: LanguageDetector,
    ) -> int:
        if self.sample_rate <= 0:
            return 0
        selected = []
        tuning_selected = 0
        benchmark_selected = 0
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
            tuning_capacity = self.tuning_written + tuning_selected < self.max_samples
            selected_for_coverage = rank < self.sample_rate and (
                self.representative or tuning_capacity
            )
            selected_for_uncertainty = (
                uncertainty >= 0.5
                and uncertain_rank < EVALUATION_UNCERTAIN_SAMPLE_RATE
                and tuning_capacity
            )
            minority_rank = int(sample_id[32:48], 16) / float(2**64)
            selected_for_minority_probe = (
                minority_rank < EVALUATION_MINORITY_PROBE_RATE
                and tuning_capacity
            )
            if (
                not selected_for_coverage
                and not selected_for_uncertainty
                and not selected_for_minority_probe
            ):
                continue
            existing_role = self._known_roles.get(sample_id)
            can_upgrade = (
                self.representative
                and selected_for_coverage
                and existing_role not in {None, "benchmark"}
            )
            if existing_role is not None and not can_upgrade:
                continue
            language, confidence = language_detector.detect(paragraph.text)
            selected_for_minority = (
                selected_for_minority_probe
                and language not in {"en", "unknown"}
                and self._language_written[language]
                < EVALUATION_MINORITY_LANGUAGE_QUOTA
            )
            if (
                not selected_for_coverage
                and not selected_for_uncertainty
                and not selected_for_minority
            ):
                continue
            sample_role = (
                "benchmark"
                if selected_for_coverage and self.representative
                else "tuning"
            )
            sampling_probability = (
                self.sample_rate
                if sample_role == "benchmark"
                else None
            )
            selected.append(
                {
                    "schema_version": OUTPUT_SCHEMA_VERSION,
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
                        "coverage"
                        if selected_for_coverage
                        else (
                            "decision_boundary"
                            if selected_for_uncertainty
                            else "minority_language"
                        )
                    ),
                    "sampling_stratum": (
                        "filter_accept" if decision.accepted else "filter_reject"
                    ),
                    "sample_role": sample_role,
                    "evaluation_split": _evaluation_split(sample_id, sample_role),
                    "sampling_probability": sampling_probability,
                    "sample_weight": (
                        round(1.0 / sampling_probability, 6)
                        if sampling_probability
                        else None
                    ),
                    "predicted_content_category": classify_content(
                        paragraph.text, paragraph.url
                    ).category,
                }
            )
            if paragraph.raw_text and paragraph.raw_text != paragraph.text:
                selected[-1]["raw_paragraph"] = paragraph.raw_text
            self._known.add(sample_id)
            self._known_roles[sample_id] = sample_role
            self._language_written[language] += 1
            if sample_role == "benchmark":
                benchmark_selected += 1
            else:
                tuning_selected += 1

        if selected:
            with self.path.open("a", encoding="utf-8") as handle:
                for row in selected:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.written += len(selected)
            self.tuning_written += tuning_selected
            self.benchmark_written += benchmark_selected
        return len(selected)

    def observe_shadow(
        self,
        paragraphs: list,
        population_size: int,
        language_detector: LanguageDetector,
        source_probability: float = 1.0,
    ) -> int:
        """Persist a representative reservoir of paragraphs rejected by keywords."""
        if self.sample_rate <= 0 or not paragraphs or population_size <= 0:
            return 0
        selected = []
        within_source_probability = min(1.0, len(paragraphs) / population_size)
        probability = source_probability * within_source_probability
        if self.representative and not 0 < probability <= 1:
            raise ValueError("shadow sampling probability must be between 0 and 1")
        for paragraph in paragraphs:
            sample_id = stable_record_id(
                paragraph.crawl_id,
                paragraph.source_file,
                paragraph.url,
                paragraph.warc_date,
                paragraph.text,
            )
            existing_role = self._known_roles.get(sample_id)
            can_upgrade = self.representative and existing_role not in {
                None,
                "benchmark",
            }
            if existing_role is not None and not can_upgrade:
                continue
            language, confidence = language_detector.detect(paragraph.text)
            row = {
                "schema_version": OUTPUT_SCHEMA_VERSION,
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
                "matched_keywords": [],
                "semantic_score": None,
                "concept_match": "",
                "narrative_score": None,
                "predicted_accept": False,
                "rejection_reason": "keyword_prefilter",
                "uncertainty_score": 0.0,
                "selection_reason": (
                    "pre_keyword_reservoir"
                    if self.representative
                    else "audit_pre_keyword_reservoir"
                ),
                "sampling_stratum": "keyword_reject",
                "sample_role": "benchmark" if self.representative else "tuning",
                "evaluation_split": _evaluation_split(
                    sample_id,
                    "benchmark" if self.representative else "tuning",
                ),
                "sampling_probability": probability if self.representative else None,
                "sample_weight": (
                    round(1.0 / probability, 6) if self.representative else None
                ),
                "predicted_content_category": classify_content(
                    paragraph.text, paragraph.url
                ).category,
            }
            if paragraph.raw_text and paragraph.raw_text != paragraph.text:
                row["raw_paragraph"] = paragraph.raw_text
            selected.append(row)
            self._known.add(sample_id)
            self._known_roles[sample_id] = str(row["sample_role"])
        if selected:
            with self.path.open("a", encoding="utf-8") as handle:
                for row in selected:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.written += len(selected)
            self.benchmark_written += len(selected)
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


def _representative_pick(rows: Iterable[dict], limit: int) -> list[dict]:
    """Take a uniform deterministic reservoir and retain its inclusion weight."""
    if limit <= 0:
        return []
    heap: list[tuple[int, int, dict]] = []
    total = 0
    for sequence, source_row in enumerate(rows):
        total += 1
        row = _enrich_for_active_learning(source_row)
        item = (-_rank(row), sequence, row)
        if len(heap) < limit:
            heapq.heappush(heap, item)
        elif item > heap[0]:
            heapq.heapreplace(heap, item)
    selected = [item[2] for item in sorted(heap, key=lambda item: (-item[0], item[1]))]
    reservoir_probability = min(1.0, len(selected) / total) if total else 0.0
    for row in selected:
        previous_probability = row.get("sampling_probability")
        try:
            previous_probability = float(previous_probability)
        except (TypeError, ValueError):
            previous_probability = 1.0
        probability = max(0.0, min(1.0, previous_probability * reservoir_probability))
        row["sample_role"] = "benchmark"
        row["evaluation_split"] = _evaluation_split(str(row["sample_id"]), "benchmark")
        row["sampling_probability"] = probability or None
        row["sample_weight"] = round(1.0 / probability, 6) if probability else None
    return selected


def _as_tuning_rows(rows: Iterable[dict]) -> Iterator[dict]:
    for row in rows:
        row["sample_role"] = "tuning"
        row["evaluation_split"] = "tuning"
        row["sampling_probability"] = None
        row["sample_weight"] = None
        yield row


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

    benchmark = [
        row for row in merged.values() if row.get("sample_role") == "benchmark"
    ]
    tuning = [
        row for row in merged.values() if row.get("sample_role") != "benchmark"
    ]
    tuning_reserve = min(len(tuning), max_samples // 4)
    selected = _representative_pick(benchmark, max_samples - tuning_reserve)
    remaining = max_samples - len(selected)
    selected.extend(_active_learning_pick(_as_tuning_rows(tuning), remaining))
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
        "benchmark": sum(row.get("sample_role") == "benchmark" for row in selected),
        "tuning": sum(row.get("sample_role") == "tuning" for row in selected),
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
            "schema_version": int(record.get("schema_version", OUTPUT_SCHEMA_VERSION)),
            "sample_id": sample_id,
            "sample_origin": "committed_output",
            "crawl_id": record.get("crawl_id", ""),
            "source_file": source_file,
            "url": record.get("url", ""),
            "warc_date": record.get("warc_date", ""),
            "language": record.get("language", "unknown"),
            "language_confidence": record.get("language_confidence", 0.0),
            "paragraph": record.get("paragraph", ""),
            "raw_paragraph": record.get("raw_paragraph", ""),
            "matched_keywords": record.get("matched_keywords", []),
            "semantic_score": record.get("semantic_score"),
            "concept_match": record.get("concept_match", ""),
            "narrative_score": record.get("narrative_score", MIN_NARRATIVE_INDICATORS),
            "predicted_accept": True,
            "rejection_reason": None,
            "sampling_stratum": "accepted_output",
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
    output_rows = _representative_pick(
        _output_annotation_rows(output_dir),
        positive_target,
    )
    candidate_rows_by_id = {
        str(row.get("sample_id", "")): row
        for row in [*_read_jsonl(Path(replay_path)), *_read_jsonl(candidate_path)]
        if row.get("sample_id")
    }
    candidate_rows = [
        _enrich_for_active_learning(row) for row in candidate_rows_by_id.values()
    ]
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
    rejected_target = size - len(output_rows)
    benchmark_rejected = [
        row for row in rejected if row.get("sample_role") == "benchmark"
    ]
    rejected_rows = _representative_pick(benchmark_rejected, rejected_target)
    selected_rejected_ids = {row["sample_id"] for row in rejected_rows}
    if len(rejected_rows) < rejected_target:
        tuning_rejected = (
            row for row in rejected if row["sample_id"] not in selected_rejected_ids
        )
        rejected_rows.extend(
            _active_learning_pick(tuning_rejected, rejected_target - len(rejected_rows))
        )

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
        for row in extras:
            if row.get("sample_role") != "benchmark":
                row["sample_role"] = "tuning"
                row["evaluation_split"] = "tuning"
        rows.extend(_active_learning_pick(extras, size - len(rows)))
    if len(rows) < size:
        selected_ids = {row["sample_id"] for row in rows}
        extra_output = (
            row
            for row in _output_annotation_rows(output_dir)
            if row["sample_id"] not in selected_ids
        )
        rows.extend(
            _active_learning_pick(_as_tuning_rows(extra_output), size - len(rows))
        )

    for row in rows:
        old = existing.get(row["sample_id"])
        if old:
            row["label"] = old.get("label")
            row["notes"] = old.get("notes", "")
            row["content_label"] = old.get("content_label")
            for key in ("annotator", "labeled_at", "label_history"):
                if old.get(key) is not None:
                    row[key] = old[key]
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
    predicted_positive = sum(bool(row.get("predicted_accept")) for row in rows)
    predicted_negative = len(rows) - predicted_positive
    balanced = predicted_positive > 0 and predicted_negative > 0
    return {
        "path": str(annotation_path),
        "samples": len(rows),
        "predicted_positive": predicted_positive,
        "predicted_negative": predicted_negative,
        "labeled": sum(row.get("label") in {"positive", "negative"} for row in rows),
        "uncertain": sum(float(row.get("uncertainty_score", 0.0)) >= 0.5 for row in rows),
        "splits": dict(
            sorted(Counter(str(row.get("evaluation_split", "tuning")) for row in rows).items())
        ),
        "sampling_strata": dict(
            sorted(Counter(str(row.get("sampling_stratum", "unknown")) for row in rows).items())
        ),
        "balanced_predictions": balanced,
        "warning": (
            None
            if balanced
            else "No rejected candidates are available; run a bounded audit before calibration."
        ),
    }


def _sampled_funnel_by_language(rows: Iterable[dict]) -> dict[str, dict]:
    funnel: dict[str, Counter] = defaultdict(Counter)
    for source_row in rows:
        row = _enrich_for_active_learning(source_row)
        language = str(row.get("language") or "unknown")
        stratum = _sampling_stratum(row)
        if stratum == "keyword_reject":
            stage = "keyword_rejected"
        elif row.get("predicted_accept"):
            stage = "accepted"
        else:
            stage = "filter_rejected"
        funnel[language]["samples"] += 1
        funnel[language][stage] += 1
        weight = _sample_weight(row)
        if row.get("sample_role") == "benchmark" and weight is not None:
            funnel[language][f"weighted_{stage}"] += weight
    return {
        language: {
            key: round(value, 3) if isinstance(value, float) else value
            for key, value in sorted(values.items())
        }
        for language, values in sorted(funnel.items())
    }


def evaluation_status(
    annotation_path: str | Path = EVALUATION_DIR / "annotations.jsonl",
    candidate_path: str | Path = EVALUATION_DIR / "candidate_samples.jsonl",
    replay_path: str | Path = REPLAY_PATH,
) -> dict:
    """Summarize evaluation readiness without requiring human labels."""
    annotations = [
        _enrich_for_active_learning(row) for row in _read_jsonl(Path(annotation_path))
    ]
    candidates_by_id = {
        str(row.get("sample_id", "")): row
        for row in [*_read_jsonl(Path(replay_path)), *_read_jsonl(Path(candidate_path))]
        if row.get("sample_id")
    }
    candidates = [
        _enrich_for_active_learning(row) for row in candidates_by_id.values()
    ]
    labels = Counter(
        str(row.get("label"))
        for row in annotations
        if row.get("label") in {"positive", "negative"}
    )
    labeled = labels["positive"] + labels["negative"]
    predicted_positive = sum(bool(row.get("predicted_accept")) for row in annotations)
    predicted_negative = len(annotations) - predicted_positive
    candidate_positive = sum(bool(row.get("predicted_accept")) for row in candidates)
    candidate_negative = len(candidates) - candidate_positive
    baseline_ready = (
        labeled >= EVALUATION_MIN_BASELINE_LABELS
        and labels["positive"] > 0
        and labels["negative"] > 0
    )
    holdout_rows = [
        row
        for row in annotations
        if row.get("sample_role") == "benchmark"
        and row.get("evaluation_split") == "holdout"
        and row.get("label") in {"positive", "negative"}
    ]
    holdout_labels = Counter(str(row["label"]) for row in holdout_rows)
    holdout_ready = (
        len(holdout_rows) >= EVALUATION_MIN_HOLDOUT_LABELS
        and holdout_labels["positive"] > 0
        and holdout_labels["negative"] > 0
    )
    keyword_shadow = sum(
        _sampling_stratum(row) == "keyword_reject" for row in candidates
    )
    representative_keyword_shadow = sum(
        _sampling_stratum(row) == "keyword_reject"
        and row.get("sample_role") == "benchmark"
        and _sample_weight(row) is not None
        for row in candidates
    )

    next_actions = []
    if not annotations:
        next_actions.append("Build a sample with `python main.py evaluation sample`.")
    if predicted_negative == 0:
        if candidate_negative:
            next_actions.append(
                "Rebuild the sample to include the available rejected candidates."
            )
        else:
            next_actions.append(
                "Run a bounded audit to collect rejected candidates before calibration."
            )
    if annotations and labeled < EVALUATION_MIN_BASELINE_LABELS:
        next_actions.append(
            f"Label at least {EVALUATION_MIN_BASELINE_LABELS - labeled} more samples."
        )
    if labeled and not labels["positive"]:
        next_actions.append("Add at least one human-labeled positive sample.")
    if labeled and not labels["negative"]:
        next_actions.append("Add at least one human-labeled negative sample.")
    if baseline_ready and predicted_negative:
        next_actions.append("Generate the evaluation report and review threshold changes.")
    if not keyword_shadow:
        next_actions.append(
            "Run a bounded audit to inspect pre-keyword rejects, then continue a normal crawl."
        )
    elif not representative_keyword_shadow:
        next_actions.append(
            "Continue a normal crawl to collect representative pre-keyword recall evidence."
        )
    if baseline_ready and not holdout_ready:
        next_actions.append(
            f"Label at least {max(0, EVALUATION_MIN_HOLDOUT_LABELS - len(holdout_rows))} "
            "more representative holdout samples."
        )

    return {
        "schema_version": 2,
        "annotation_path": str(Path(annotation_path)),
        "samples": len(annotations),
        "labeled": labeled,
        "unlabeled": len(annotations) - labeled,
        "labels": {
            "positive": labels["positive"],
            "negative": labels["negative"],
        },
        "sample_predictions": {
            "accepted": predicted_positive,
            "rejected": predicted_negative,
            "balanced": predicted_positive > 0 and predicted_negative > 0,
        },
        "candidate_reservoir": {
            "samples": len(candidates),
            "accepted": candidate_positive,
            "rejected": candidate_negative,
            "keyword_shadow": keyword_shadow,
            "representative_keyword_shadow": representative_keyword_shadow,
        },
        "origins": dict(
            sorted(Counter(str(row.get("sample_origin", "unknown")) for row in annotations).items())
        ),
        "splits": dict(
            sorted(
                Counter(
                    str(row.get("evaluation_split", "tuning")) for row in annotations
                ).items()
            )
        ),
        "sampling_strata": dict(
            sorted(
                Counter(
                    str(row.get("sampling_stratum", "unknown")) for row in annotations
                ).items()
            )
        ),
        "sampled_funnel_by_language": _sampled_funnel_by_language(candidates),
        "baseline": {
            "ready": baseline_ready,
            "minimum_labels": EVALUATION_MIN_BASELINE_LABELS,
            "requires_both_classes": True,
            "holdout_ready": holdout_ready,
            "holdout_labels": len(holdout_rows),
            "minimum_holdout_labels": EVALUATION_MIN_HOLDOUT_LABELS,
        },
        "next_actions": next_actions,
    }


def evaluation_plan(
    annotation_path: str | Path = EVALUATION_DIR / "annotations.jsonl",
) -> dict:
    """Build a balanced human-labeling plan without assigning any labels."""
    path = Path(annotation_path)
    rows = [_enrich_for_active_learning(row) for row in _read_jsonl(path)]
    holdout_each = max(1, EVALUATION_MIN_HOLDOUT_LABELS // 2)
    tuning_total = max(0, EVALUATION_MIN_BASELINE_LABELS - holdout_each * 2)
    tuning_rejected = tuning_total // 2
    tuning_accepted = tuning_total - tuning_rejected
    quotas = [
        ("holdout", False, holdout_each),
        ("holdout", True, holdout_each),
        ("tuning", False, tuning_rejected),
        ("tuning", True, tuning_accepted),
    ]
    steps = []
    total_remaining = 0
    insufficient = []
    for split, accepted, target in quotas:
        matching = [
            row
            for row in rows
            if row.get("evaluation_split", "tuning") == split
            and bool(row.get("predicted_accept")) is accepted
        ]
        labeled = sum(row.get("label") in {"positive", "negative"} for row in matching)
        available = sum(row.get("label") not in {"positive", "negative"} for row in matching)
        remaining = max(0, target - labeled)
        planned = min(remaining, available)
        total_remaining += remaining
        prediction = "accepted" if accepted else "rejected"
        if planned:
            steps.append(
                {
                    "split": split,
                    "prediction": prediction,
                    "target": target,
                    "labeled": labeled,
                    "available": available,
                    "planned": planned,
                    "command": (
                        "python main.py evaluation annotate "
                        f"--split {split} --prediction {prediction} "
                        f"--limit {planned} --quick"
                    ),
                }
            )
        if available < remaining:
            insufficient.append(
                {
                    "split": split,
                    "prediction": prediction,
                    "needed": remaining,
                    "available": available,
                }
            )
    status = evaluation_status(annotation_path=path)
    return {
        "schema_version": 1,
        "annotation_path": str(path),
        "requires_human_judgment": True,
        "automatic_labeling_allowed": False,
        "target_labels": EVALUATION_MIN_BASELINE_LABELS,
        "target_holdout_labels": EVALUATION_MIN_HOLDOUT_LABELS,
        "remaining_to_target": total_remaining,
        "ready": status["baseline"]["ready"] and status["baseline"]["holdout_ready"],
        "steps": steps,
        "insufficient_queues": insufficient,
        "browser_command": "python main.py evaluation serve --open-browser",
        "next_actions": status["next_actions"],
    }


def annotate(
    annotation_path: str | Path = EVALUATION_DIR / "annotations.jsonl",
    language: str | None = None,
    limit: int | None = None,
    predicted_accept: bool | None = None,
    split: str | None = None,
    sample_id: str | None = None,
    relabel: bool = False,
    annotator: str | None = None,
    quick: bool = False,
    input_func=input,
) -> dict:
    path = Path(annotation_path)
    rows = _read_jsonl(path)
    if not rows:
        raise FileNotFoundError(f"No annotation sample at {path}; run evaluation sample first")

    if split not in {None, "all", "tuning", "holdout"}:
        raise ValueError("split must be tuning, holdout, or all")
    annotator = annotator or os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    eligible = []
    for index, row in enumerate(rows, start=1):
        if not relabel and row.get("label") in {"positive", "negative"}:
            continue
        if sample_id and row.get("sample_id") != sample_id:
            continue
        if language and row.get("language") != language:
            continue
        if predicted_accept is not None and bool(row.get("predicted_accept")) != predicted_accept:
            continue
        if split not in {None, "all"} and row.get("evaluation_split", "tuning") != split:
            continue
        eligible.append((index, row))

    buckets: dict[tuple[bool, str], list[tuple[int, dict]]] = defaultdict(list)
    for item in eligible:
        row = item[1]
        buckets[(bool(row.get("predicted_accept")), str(row.get("language", "unknown")))].append(
            item
        )
    ordered = []
    while True:
        added = False
        for key in sorted(buckets, key=lambda value: (value[0], value[1])):
            if buckets[key]:
                ordered.append(buckets[key].pop(0))
                added = True
        if not added:
            break

    labeled_now = 0
    session_changes: list[tuple[dict, dict]] = []
    for index, row in ordered:
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
        answer = input_func(
            "\n[p]ositive [n]egative [t]note [u]ndo [s]kip [q]uit: "
        ).strip().lower()
        if answer == "q":
            break
        if answer == "u":
            if session_changes:
                changed_row, snapshot = session_changes.pop()
                for key in (
                    "label",
                    "content_label",
                    "annotator",
                    "labeled_at",
                    "label_history",
                ):
                    if key in snapshot:
                        changed_row[key] = snapshot[key]
                    else:
                        changed_row.pop(key, None)
                labeled_now = max(0, labeled_now - 1)
                _atomic_jsonl(path, rows)
            continue
        if answer == "p":
            new_label = "positive"
        elif answer == "n":
            new_label = "negative"
        elif answer == "t":
            row["notes"] = input_func("Note: ").strip()
            row["notes_updated_at"] = _utc_now()
            _atomic_jsonl(path, rows)
            continue
        else:
            continue
        snapshot = {
            key: row[key]
            for key in (
                "label",
                "content_label",
                "annotator",
                "labeled_at",
                "label_history",
            )
            if key in row
        }
        history = list(row.get("label_history") or [])
        history.append(
            {
                "label": row.get("label"),
                "content_label": row.get("content_label"),
                "annotator": row.get("annotator"),
                "labeled_at": row.get("labeled_at"),
                "changed_at": _utc_now(),
            }
        )
        row["label_history"] = history[-20:]
        row["label"] = new_label
        row["annotator"] = annotator
        row["labeled_at"] = _utc_now()
        category_codes = {
            "p": "personal_prose",
            "l": "lyrics",
            "o": "poetry",
            "c": "commercial",
            "g": "genealogy",
            "a": "adult_content",
            "u": "unknown",
        }
        category = ""
        if not quick:
            category = input_func(
                "Content [p]ersonal [l]yrics p[o]etry [c]ommercial "
                "[g]enealogy [a]dult [u]nknown [Enter=model]: "
            ).strip().lower()
        row["content_label"] = category_codes.get(
            category,
            row.get("predicted_content_category", "unknown"),
        )
        session_changes.append((row, snapshot))
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
        "annotator": annotator,
    }


def undo_annotation(
    annotation_path: str | Path = EVALUATION_DIR / "annotations.jsonl",
    sample_id: str | None = None,
) -> dict:
    """Undo the latest label, or the latest label for one explicit sample."""
    path = Path(annotation_path)
    with _ANNOTATION_LOCK:
        rows = _read_jsonl(path)
        candidates = [
            row
            for row in rows
            if row.get("label") in {"positive", "negative"}
            and (sample_id is None or row.get("sample_id") == sample_id)
        ]
        if not candidates:
            raise ValueError("no matching labeled sample is available to undo")
        row = max(
            candidates,
            key=lambda value: (
                str(value.get("labeled_at", "")),
                str(value.get("sample_id", "")),
            ),
        )
        history = list(row.get("label_history") or [])
        previous = history.pop() if history else {}
        for key in ("label", "content_label", "annotator", "labeled_at"):
            value = previous.get(key)
            if value is None:
                row.pop(key, None)
            else:
                row[key] = value
        if history:
            row["label_history"] = history
        else:
            row.pop("label_history", None)
        _atomic_jsonl(path, rows)
        return {
            "path": str(path),
            "sample_id": row.get("sample_id"),
            "restored_label": row.get("label"),
            "labeled_total": sum(
                item.get("label") in {"positive", "negative"} for item in rows
            ),
        }


def annotation_queue(
    annotation_path: str | Path = EVALUATION_DIR / "annotations.jsonl",
    language: str | None = None,
    predicted_accept: bool | None = None,
    split: str | None = None,
    relabel: bool = False,
) -> list[dict]:
    """Return a stable, language-balanced annotation queue for any UI."""
    if split not in {None, "all", "tuning", "holdout"}:
        raise ValueError("split must be tuning, holdout, or all")
    rows = [
        _enrich_for_active_learning(row)
        for row in _read_jsonl(Path(annotation_path))
        if (relabel or row.get("label") not in {"positive", "negative"})
        and (not language or row.get("language") == language)
        and (
            predicted_accept is None
            or bool(row.get("predicted_accept")) == predicted_accept
        )
        and (
            split in {None, "all"}
            or row.get("evaluation_split", "tuning") == split
        )
    ]
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in sorted(
        rows,
        key=lambda value: (
            -float(value.get("uncertainty_score", 0.0)),
            _rank(value),
        ),
    ):
        buckets[str(row.get("language") or "unknown")].append(row)
    ordered = []
    while True:
        added = False
        for bucket in sorted(buckets):
            if buckets[bucket]:
                ordered.append(buckets[bucket].pop(0))
                added = True
        if not added:
            return ordered


def label_annotation(
    sample_id: str,
    label: str,
    content_label: str | None = None,
    notes: str | None = None,
    annotator: str | None = None,
    annotation_path: str | Path = EVALUATION_DIR / "annotations.jsonl",
    expected_labeled_at: str | None = None,
) -> dict:
    """Atomically label one row while preserving bounded annotation history."""
    if label not in {"positive", "negative"}:
        raise ValueError("label must be positive or negative")
    allowed_content = {
        "personal_prose",
        "lyrics",
        "poetry",
        "commercial",
        "genealogy",
        "adult_content",
        "unknown",
    }
    if content_label is not None and content_label not in allowed_content:
        raise ValueError("unknown content label")
    path = Path(annotation_path)
    with _ANNOTATION_LOCK:
        rows = _read_jsonl(path)
        row = next(
            (item for item in rows if str(item.get("sample_id")) == sample_id),
            None,
        )
        if row is None:
            raise ValueError(f"unknown sample id: {sample_id}")
        if (
            expected_labeled_at is not None
            and row.get("labeled_at") != expected_labeled_at
        ):
            raise RuntimeError("annotation changed since it was loaded")
        changed_at = _utc_now()
        history = list(row.get("label_history") or [])
        history.append(
            {
                "label": row.get("label"),
                "content_label": row.get("content_label"),
                "annotator": row.get("annotator"),
                "labeled_at": row.get("labeled_at"),
                "changed_at": changed_at,
            }
        )
        row["label_history"] = history[-20:]
        row["label"] = label
        row["content_label"] = (
            content_label
            or row.get("content_label")
            or row.get("predicted_content_category")
            or "unknown"
        )
        row["annotator"] = (annotator or "local-workbench").strip()[:120]
        row["labeled_at"] = changed_at
        if notes is not None:
            row["notes"] = notes.strip()[:4000]
            row["notes_updated_at"] = changed_at
        _atomic_jsonl(path, rows)
        return {
            "sample_id": sample_id,
            "label": label,
            "content_label": row["content_label"],
            "labeled_at": changed_at,
            "labeled_total": sum(
                item.get("label") in {"positive", "negative"} for item in rows
            ),
            "remaining": sum(
                item.get("label") not in {"positive", "negative"} for item in rows
            ),
        }


def multilingual_recall_report(
    annotation_path: str | Path = EVALUATION_DIR / "annotations.jsonl",
    candidate_path: str | Path = EVALUATION_DIR / "candidate_samples.jsonl",
    replay_path: str | Path = REPLAY_PATH,
    report_path: str | Path = EVALUATION_DIR / "multilingual-report.json",
) -> dict:
    """Report language evidence, native-anchor gaps, and labeled keyword misses."""
    from concepts import CONCEPT_ANCHOR_LANGUAGES
    from keywords import KEYWORDS_BY_LANGUAGE

    annotations = [
        _enrich_for_active_learning(row) for row in _read_jsonl(Path(annotation_path))
    ]
    candidates_by_id = {
        str(row.get("sample_id", "")): _enrich_for_active_learning(row)
        for row in [*_read_jsonl(Path(replay_path)), *_read_jsonl(Path(candidate_path))]
        if row.get("sample_id")
    }
    configured = set(KEYWORDS_BY_LANGUAGE)
    anchored = set(CONCEPT_ANCHOR_LANGUAGES)
    observed = {
        str(row.get("language") or "unknown")
        for row in [*annotations, *candidates_by_id.values()]
    }
    languages = {}
    keyword_misses = []
    for language in sorted(configured | anchored | observed):
        annotation_rows = [
            row for row in annotations if str(row.get("language") or "unknown") == language
        ]
        candidate_rows = [
            row
            for row in candidates_by_id.values()
            if str(row.get("language") or "unknown") == language
        ]
        labeled = [
            row for row in annotation_rows if row.get("label") in {"positive", "negative"}
        ]
        misses = [
            row
            for row in labeled
            if row.get("label") == "positive"
            and _sampling_stratum(row) == "keyword_reject"
        ]
        keyword_misses.extend(misses)
        positives = sum(row.get("label") == "positive" for row in labeled)
        languages[language] = {
            "keyword_terms": len(KEYWORDS_BY_LANGUAGE.get(language, [])),
            "native_anchors": CONCEPT_ANCHOR_LANGUAGES.count(language),
            "candidate_samples": len(candidate_rows),
            "annotation_samples": len(annotation_rows),
            "labeled": len(labeled),
            "positive": positives,
            "negative": len(labeled) - positives,
            "keyword_shadow": sum(
                _sampling_stratum(row) == "keyword_reject" for row in candidate_rows
            ),
            "keyword_miss_positives": len(misses),
            "ready_for_language_calibration": (
                len(labeled) >= EVALUATION_MIN_LANGUAGE_LABELS
                and 0 < positives < len(labeled)
            ),
        }

    payload = {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "configured_keyword_languages": len(configured),
        "native_anchor_languages": len(anchored),
        "observed_languages": len(observed),
        "languages_missing_native_anchor": sorted(configured - anchored),
        "languages_without_samples": sorted(
            language
            for language in configured
            if not languages[language]["candidate_samples"]
            and not languages[language]["annotation_samples"]
        ),
        "languages_ready_for_calibration": sorted(
            language
            for language, values in languages.items()
            if values["ready_for_language_calibration"]
        ),
        "labeled_keyword_misses": len(keyword_misses),
        "keyword_miss_examples": [
            {
                "sample_id": row.get("sample_id"),
                "language": row.get("language"),
                "url": row.get("url"),
                "paragraph": str(row.get("paragraph", ""))[:500],
            }
            for row in sorted(keyword_misses, key=_rank)[:25]
        ],
        "languages": languages,
    }
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, report_path)
    return payload


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


def _weighted_classification(
    rows: list[dict],
    semantic: float | None = None,
    narrative: int | None = None,
) -> dict | None:
    """Estimate population metrics from representative rows with known inclusion odds."""
    confusion = {"tp": 0.0, "fp": 0.0, "tn": 0.0, "fn": 0.0}
    samples = 0
    for row in rows:
        if row.get("sample_role") != "benchmark":
            continue
        weight = _sample_weight(row)
        if weight is None:
            continue
        actual = row["label"] == "positive"
        if semantic is None or narrative is None:
            predicted = bool(row.get("predicted_accept"))
        else:
            score = row.get("semantic_score")
            narrative_score = row.get("narrative_score")
            if score is None or narrative_score is None:
                continue
            predicted = float(score) >= semantic and int(narrative_score) >= narrative
        samples += 1
        if predicted and actual:
            confusion["tp"] += weight
        elif predicted:
            confusion["fp"] += weight
        elif actual:
            confusion["fn"] += weight
        else:
            confusion["tn"] += weight
    if not samples:
        return None
    tp, fp, fn = confusion["tp"], confusion["fp"], confusion["fn"]
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "samples": samples,
        "represented_population": round(sum(confusion.values()), 3),
        **{key: round(value, 3) for key, value in confusion.items()},
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "confidence_intervals": None,
        "warning": "Survey-weighted confidence intervals require more representative labels.",
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
    all_rows = [
        _enrich_for_active_learning(row) for row in _read_jsonl(Path(annotation_path))
    ]
    rows = [
        row
        for row in all_rows
        if row.get("label") in {"positive", "negative"}
    ]
    if not rows:
        status = evaluation_status(annotation_path=annotation_path)
        payload = {
            "schema_version": 3,
            "generated_at": _utc_now(),
            "human_labeled_samples": 0,
            "unlabeled_samples": len(all_rows),
            "label_balance": {"positive": 0, "negative": 0},
            "baseline": {
                "ready": False,
                "minimum_labels": EVALUATION_MIN_BASELINE_LABELS,
                "requires_both_classes": True,
                "warning": "No human-labeled rows are available yet.",
            },
            "overall": None,
            "weighted_benchmark": None,
            "metric_scope": "not_ready",
            "tuning": {"samples": 0, "labeled": 0},
            "holdout": {
                "samples": 0,
                "labeled": 0,
                "ready": False,
                "minimum_labels": EVALUATION_MIN_HOLDOUT_LABELS,
            },
            "by_language": {},
            "semantic_calibration": [],
            "content_taxonomy": {
                "human_labeled": {},
                "predicted": {},
                "agreement": None,
            },
            "recommended_thresholds": None,
            "false_positives": [],
            "false_negatives": [],
            "status": status,
        }
        report_path = Path(report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = report_path.with_suffix(report_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, report_path)
        return payload

    positives = sum(row["label"] == "positive" for row in rows)
    negatives = len(rows) - positives
    baseline_ready = (
        len(rows) >= EVALUATION_MIN_BASELINE_LABELS and positives > 0 and negatives > 0
    )
    tuning_rows = [
        row
        for row in rows
        if not (
            row.get("sample_role") == "benchmark"
            and row.get("evaluation_split") == "holdout"
        )
    ]
    holdout_rows = [
        row
        for row in rows
        if row.get("sample_role") == "benchmark"
        and row.get("evaluation_split") == "holdout"
    ]
    holdout_positives = sum(row["label"] == "positive" for row in holdout_rows)
    holdout_ready = (
        len(holdout_rows) >= EVALUATION_MIN_HOLDOUT_LABELS
        and 0 < holdout_positives < len(holdout_rows)
    )
    tunable_rows = [
        row
        for row in tuning_rows
        if row.get("semantic_score") is not None
        and row.get("narrative_score") is not None
    ]
    benchmark_rows = [
        row
        for row in rows
        if row.get("sample_role") == "benchmark" and _sample_weight(row) is not None
    ]
    labeled_strata = {
        _sampling_stratum(row) for row in benchmark_rows
    }
    if {"accepted_output", "filter_reject", "keyword_reject"} <= labeled_strata:
        metric_scope = "end_to_end_weighted"
    elif {"accepted_output", "filter_reject"} <= labeled_strata:
        metric_scope = "downstream_filter_weighted"
    else:
        metric_scope = "descriptive_active_sample"
    by_language = {}
    for language in sorted({str(row.get("language", "unknown")) for row in rows}):
        language_rows = [row for row in rows if str(row.get("language", "unknown")) == language]
        language_positives = sum(row["label"] == "positive" for row in language_rows)
        language_tuning = [
            row
            for row in language_rows
            if row in tunable_rows
        ]
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
                _best_threshold(language_tuning)
                if language_ready and language_tuning
                else None
            ),
        }

    best = _best_threshold(tunable_rows) if tunable_rows else None
    holdout_scored = [
        row
        for row in holdout_rows
        if row.get("semantic_score") is not None
        and row.get("narrative_score") is not None
    ]
    holdout_validation = (
        _classification(
            holdout_scored,
            best["semantic_threshold"],
            best["narrative_threshold"],
        )
        if best and holdout_scored
        else None
    )
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
        "schema_version": 3,
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
            "holdout_ready": holdout_ready,
            "minimum_holdout_labels": EVALUATION_MIN_HOLDOUT_LABELS,
            "warning": (
                None
                if baseline_ready and holdout_ready
                else "Threshold recommendations remain exploratory until both baseline and holdout are ready."
            ),
        },
        "overall": _classification(rows),
        "weighted_benchmark": _weighted_classification(benchmark_rows),
        "metric_scope": metric_scope,
        "sampling": {
            "roles": dict(
                sorted(Counter(str(row.get("sample_role", "legacy")) for row in rows).items())
            ),
            "splits": dict(
                sorted(Counter(str(row.get("evaluation_split", "tuning")) for row in rows).items())
            ),
            "strata": dict(
                sorted(Counter(_sampling_stratum(row) for row in rows).items())
            ),
        },
        "tuning": {
            "samples": len(tuning_rows),
            "scored_samples": len(tunable_rows),
            "metrics": _classification(tuning_rows),
        },
        "holdout": {
            "samples": len(holdout_rows),
            "ready": holdout_ready,
            "minimum_labels": EVALUATION_MIN_HOLDOUT_LABELS,
            "metrics": _classification(holdout_rows) if holdout_rows else None,
            "recommended_threshold_validation": holdout_validation,
        },
        "by_language": by_language,
        "semantic_calibration": _calibration(rows),
        "content_taxonomy": {
            "human_labeled": dict(sorted(human_categories.items())),
            "predicted": dict(sorted(predicted_categories.items())),
            "agreement": round(category_agreement, 4)
            if category_agreement is not None
            else None,
        },
        "recommended_thresholds": (
            {
                **best,
                "exploratory": not (baseline_ready and holdout_ready),
                "validated_on_holdout": holdout_ready,
            }
            if best
            else None
        ),
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
