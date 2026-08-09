"""Capture and compare semantic-model outputs before dependency upgrades."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from config import (
    EVALUATION_DIR,
    MODEL_BASELINE_PATH,
    SEMANTIC_MODEL_NAME,
    SEMANTIC_MODEL_REVISION,
    SEMANTIC_THRESHOLD,
    get_hardware_profile,
)
from matcher import SemanticMatcher
from signatures import current_git_commit


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _library_versions() -> dict[str, str | None]:
    result = {}
    for package in ("sentence-transformers", "torch", "transformers"):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = None
    return result


def _annotation_rows(path: Path, limit: int | None) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("sample_id") and row.get("paragraph"):
            rows.append(row)
    rows.sort(key=lambda row: str(row["sample_id"]))
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        raise ValueError(f"no scorable evaluation samples found in {path}")
    return rows


def capture_model_snapshot(
    annotation_path: str | Path = EVALUATION_DIR / "annotations.jsonl",
    output_path: str | Path = MODEL_BASELINE_PATH,
    profile_name: str = "auto",
    limit: int | None = None,
) -> dict:
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    annotation = Path(annotation_path)
    rows = _annotation_rows(annotation, limit)
    profile = get_hardware_profile(profile_name)
    matcher = SemanticMatcher(
        encoding_batch_size=profile.encoding_batch_size,
        precision=profile.precision,
    )
    scores = matcher.score_paragraphs([str(row["paragraph"]) for row in rows])
    samples = [
        {
            "sample_id": str(row["sample_id"]),
            "semantic_score": round(float(score), 8),
            "concept_match": concept,
            "above_threshold": score >= SEMANTIC_THRESHOLD,
        }
        for row, (score, concept) in zip(rows, scores)
    ]
    sample_digest = hashlib.sha256(
        json.dumps(samples, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": current_git_commit(),
        "profile": profile.name,
        "model": {
            "name": SEMANTIC_MODEL_NAME,
            "revision": SEMANTIC_MODEL_REVISION,
            "semantic_threshold": SEMANTIC_THRESHOLD,
        },
        "libraries": _library_versions(),
        "annotation_path": str(annotation),
        "samples": samples,
        "sample_count": len(samples),
        "sample_digest": sample_digest,
    }
    _atomic_json(Path(output_path), payload)
    return payload


def compare_model_snapshots(
    baseline_path: str | Path,
    candidate_path: str | Path,
    output_path: str | Path | None = None,
    max_score_drift: float = 0.005,
    minimum_concept_agreement: float = 0.99,
    minimum_threshold_agreement: float = 1.0,
) -> dict:
    if max_score_drift < 0:
        raise ValueError("max_score_drift cannot be negative")
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    candidate = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
    return compare_model_payloads(
        baseline,
        candidate,
        baseline_label=str(baseline_path),
        candidate_label=str(candidate_path),
        output_path=output_path,
        max_score_drift=max_score_drift,
        minimum_concept_agreement=minimum_concept_agreement,
        minimum_threshold_agreement=minimum_threshold_agreement,
    )


def compare_model_payloads(
    baseline: dict,
    candidate: dict,
    *,
    baseline_label: str = "baseline payload",
    candidate_label: str = "candidate payload",
    output_path: str | Path | None = None,
    max_score_drift: float = 0.005,
    minimum_concept_agreement: float = 0.99,
    minimum_threshold_agreement: float = 1.0,
) -> dict:
    """Compare already-validated payloads, including portable profile evidence."""
    if max_score_drift < 0:
        raise ValueError("max_score_drift cannot be negative")
    baseline_rows = {row["sample_id"]: row for row in baseline.get("samples", [])}
    candidate_rows = {row["sample_id"]: row for row in candidate.get("samples", [])}
    missing = sorted(set(baseline_rows) - set(candidate_rows))
    added = sorted(set(candidate_rows) - set(baseline_rows))
    shared = sorted(set(baseline_rows) & set(candidate_rows))
    if not shared:
        raise ValueError("model snapshots have no shared samples")

    differences = [
        abs(
            float(baseline_rows[sample_id]["semantic_score"])
            - float(candidate_rows[sample_id]["semantic_score"])
        )
        for sample_id in shared
    ]
    concept_agreement = sum(
        baseline_rows[sample_id]["concept_match"]
        == candidate_rows[sample_id]["concept_match"]
        for sample_id in shared
    ) / len(shared)
    threshold_agreement = sum(
        bool(baseline_rows[sample_id]["above_threshold"])
        == bool(candidate_rows[sample_id]["above_threshold"])
        for sample_id in shared
    ) / len(shared)
    maximum = max(differences)
    safe = (
        not missing
        and not added
        and maximum <= max_score_drift
        and concept_agreement >= minimum_concept_agreement
        and threshold_agreement >= minimum_threshold_agreement
    )
    result = {
        "schema_version": 1,
        "safe": safe,
        "baseline": {
            "path": baseline_label,
            "libraries": baseline.get("libraries", {}),
            "git_commit": baseline.get("git_commit"),
        },
        "candidate": {
            "path": candidate_label,
            "libraries": candidate.get("libraries", {}),
            "git_commit": candidate.get("git_commit"),
        },
        "samples": {
            "shared": len(shared),
            "missing": missing,
            "added": added,
        },
        "metrics": {
            "mean_absolute_score_drift": round(sum(differences) / len(differences), 8),
            "max_absolute_score_drift": round(maximum, 8),
            "concept_agreement": round(concept_agreement, 6),
            "threshold_decision_agreement": round(threshold_agreement, 6),
        },
        "limits": {
            "max_score_drift": max_score_drift,
            "minimum_concept_agreement": minimum_concept_agreement,
            "minimum_threshold_agreement": minimum_threshold_agreement,
        },
    }
    if output_path is not None:
        _atomic_json(Path(output_path), result)
    return result
