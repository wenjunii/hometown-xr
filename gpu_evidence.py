"""Read-only comparison of the prepared three-GPU workload and model captures."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from config import MODEL_BASELINE_PATH
from evidence_bundle import portable_evidence_path, validate_evidence_bundle
from model_regression import compare_model_payloads

REQUIRED_PROFILES = ("3080", "4090", "5090")
REFERENCE_PROFILE = REQUIRED_PROFILES[0]
SAMPLE_COUNT = 400
SOURCE_COUNT = 5
WORKERS = 7
MODEL_LIBRARIES = ("torch", "sentence-transformers", "transformers")


def _is_hex(value: object, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]+", value) is not None and (
        len(value) == length
    )


def _is_number(value: object) -> bool:
    return type(value) in (int, float) and -math.inf < value < math.inf


def _is_count(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def _complete_samples(snapshot: object) -> bool:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        return False
    samples = snapshot.get("samples")
    if not isinstance(samples, list) or len(samples) != SAMPLE_COUNT:
        return False
    if not _is_count(snapshot.get("sample_count"), SAMPLE_COUNT):
        return False
    ids = set()
    for row in samples:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("sample_id"), str)
            or not row["sample_id"].strip()
            or row["sample_id"] in ids
            or not _is_number(row.get("semantic_score"))
            or not -1.000001 <= row["semantic_score"] <= 1.000001
            or not isinstance(row.get("concept_match"), str)
            or type(row.get("above_threshold")) is not bool
        ):
            return False
        ids.add(row["sample_id"])
    return True


def _check_bundle(
    payload: dict, profile: str, expected_commit: str | None, errors: list[str]
) -> bool:
    snapshot = payload.get("model_snapshot") or {}
    workload = payload.get("workload_benchmark") or {}
    commit = payload.get("git_commit")
    if payload["profile"] != profile:
        errors.append("bundle is for a different GPU profile")
    if (
        not _is_hex(commit, 40)
        or (expected_commit and commit != expected_commit)
        or any(component.get("git_commit") != commit for component in (snapshot, workload))
    ):
        errors.append("bundle, model snapshot, and workload must use the expected runtime commit")
    signature = payload.get("filter_signature")
    if not _is_hex(signature, 64) or workload.get("filter_signature") != signature:
        errors.append("bundle and workload filter signatures do not match")
    if any(
        payload.get(key) is not False
        for key in ("generated_text", "contains_source_text", "contains_credentials")
    ):
        errors.append("bundle must be sanitized validation evidence")

    if not _complete_samples(snapshot):
        errors.append(f"model snapshot must contain {SAMPLE_COUNT} distinct, valid samples")
    runtime = snapshot.get("runtime")
    if (
        not isinstance(runtime, dict)
        or not isinstance(runtime.get("device"), str)
        or not re.fullmatch(r"cuda(?::[0-9]+)?", runtime["device"])
        or runtime.get("precision") != "fp16"
    ):
        errors.append("model snapshot must be a CUDA FP16 capture")
    model = snapshot.get("model")
    if (
        not isinstance(model, dict)
        or not all(isinstance(model.get(key), str) and model[key] for key in ("name", "revision"))
        or not _is_number(model.get("semantic_threshold"))
    ):
        errors.append("model identity, revision, and semantic threshold are required")
    libraries = snapshot.get("libraries")
    if not isinstance(libraries, dict) or not all(
        isinstance(libraries.get(key), str) and libraries[key] for key in MODEL_LIBRARIES
    ):
        errors.append("all three model library versions are required")
    snapshot_valid = not errors

    workload_runtime = workload.get("runtime")
    if (
        not isinstance(workload_runtime, dict)
        or workload_runtime.get("precision") != "fp16"
        or workload.get("cache_enabled") is not False
        or workload.get("historical_state_changed") is not False
    ):
        errors.append("benchmark must use FP16, no cache, and leave historical state unchanged")
    files = workload.get("source_files")
    crawl = workload.get("crawl_id")
    if (
        not isinstance(crawl, str)
        or not crawl.strip()
        or not _is_count(workload.get("sources_per_trial"), SOURCE_COUNT)
        or not isinstance(files, list)
        or len(files) != SOURCE_COUNT
        or not all(isinstance(file, str) and file.strip() for file in files)
        or len(set(files)) != SOURCE_COUNT
    ):
        errors.append(f"benchmark must identify {SOURCE_COUNT} different sources from one crawl")
    trials = workload.get("trials")
    trial = trials[0] if isinstance(trials, list) and len(trials) == 1 else None
    if (
        not isinstance(trial, dict)
        or not _is_count(trial.get("workers"), WORKERS)
        or not _is_count(trial.get("completed"), SOURCE_COUNT)
        or not _is_count(trial.get("failed"), 0)
        or not _is_count(trial.get("process_pool_restarts"), 0)
        or trial.get("equivalent_output") is not True
        or type(trial.get("audit_matches")) is not int
        or trial["audit_matches"] < 0
        or not _is_hex(trial.get("output_digest"), 64)
    ):
        errors.append(
            f"need one successful {WORKERS}-worker trial with no failures or restarts "
            "and output equivalent to the historical matches"
        )
    return snapshot_valid


def _compare_samples(reference: dict, target: dict) -> dict:
    if not (
        {row["sample_id"] for row in reference["samples"]}
        & {row["sample_id"] for row in target["samples"]}
    ):
        return {
            "safe": False,
            "samples": {"shared": 0, "missing": SAMPLE_COUNT, "added": SAMPLE_COUNT},
            "metrics": None,
        }
    result = compare_model_payloads(reference, target)
    return {
        "safe": result["safe"],
        "samples": {
            "shared": result["samples"]["shared"],
            "missing": len(result["samples"]["missing"]),
            "added": len(result["samples"]["added"]),
        },
        "metrics": result["metrics"],
        "limits": result["limits"],
    }


def compare_gpu_evidence(
    expected_commit: str | None,
    evidence_paths: dict[str, str | Path] | None = None,
    *,
    baseline_path: str | Path = MODEL_BASELINE_PATH,
) -> dict:
    """Compare the intended pin explicitly; never select a run by date or branch HEAD."""
    paths = evidence_paths or {}
    errors = []
    pin = expected_commit.strip().lower() if expected_commit else None
    if not _is_hex(pin, 40):
        errors.append("specify the full 40-character expected runtime commit from the handoff")
        pin = None

    baseline = None
    try:
        baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
        if not _complete_samples(baseline):
            raise ValueError(f"tracked baseline must contain {SAMPLE_COUNT} distinct, valid samples")
    except (OSError, ValueError) as exc:
        errors.append(f"cannot use baseline: {exc}")
        baseline = None

    reports: dict[str, dict] = {}
    comparable: dict[str, dict] = {}
    for profile in REQUIRED_PROFILES:
        path = Path(paths.get(profile) or portable_evidence_path(profile))
        issues: list[str] = []
        row = {"path": str(path), "present": path.is_file(), "errors": issues}
        reports[profile] = row
        if not row["present"]:
            issues.append("no evidence bundle found")
            continue
        try:
            payload = validate_evidence_bundle(path)
        except (OSError, ValueError) as exc:
            issues.append(f"cannot verify bundle: {exc}")
            continue
        row.update(
            runtime_commit=payload.get("git_commit"),
            content_sha256=payload["content_sha256"],
            filter_signature=payload.get("filter_signature"),
        )
        snapshot_valid = _check_bundle(payload, profile, pin, issues)
        snapshot = payload.get("model_snapshot") or {}
        if snapshot_valid:
            row["libraries"] = snapshot["libraries"]
            if baseline is not None:
                row["baseline_comparison"] = _compare_samples(baseline, snapshot)
        if issues:
            continue
        comparable[profile] = payload
        trial = payload["workload_benchmark"]["trials"][0]
        row["workload"] = {
            key: trial[key] for key in ("workers", "completed", "audit_matches", "output_digest")
        }
        elapsed = trial.get("elapsed_seconds")
        if _is_number(elapsed) and elapsed > 0:
            row["workload"]["elapsed_seconds"] = elapsed
        if row.get("baseline_comparison") and not row["baseline_comparison"]["safe"]:
            issues.append("model comparison with the tracked baseline exceeds existing limits")

    reference = comparable.get(REFERENCE_PROFILE)
    if reference is not None:
        ref_benchmark = reference["workload_benchmark"]
        ref_trial = ref_benchmark["trials"][0]
        ref_snapshot = reference["model_snapshot"]
        for profile, payload in comparable.items():
            if profile == REFERENCE_PROFILE:
                continue
            row = reports[profile]
            issues = row["errors"]
            benchmark = payload["workload_benchmark"]
            trial = benchmark["trials"][0]
            snapshot = payload["model_snapshot"]
            if payload["git_commit"] != reference["git_commit"]:
                issues.append("runtime commit differs from the 3080 reference")
            if payload["filter_signature"] != reference["filter_signature"]:
                issues.append("filter signature differs from the 3080 reference")
            if snapshot["model"] != ref_snapshot["model"]:
                issues.append("model identity differs from the 3080 reference")
            if snapshot["libraries"] != ref_snapshot["libraries"]:
                issues.append("model library versions differ from the 3080 reference")
            if (
                benchmark["crawl_id"] != ref_benchmark["crawl_id"]
                or set(benchmark["source_files"]) != set(ref_benchmark["source_files"])
            ):
                issues.append("benchmark sources differ from the 3080 reference")
            if any(trial[key] != ref_trial[key] for key in ("audit_matches", "output_digest")):
                issues.append("benchmark output differs from the 3080 reference")
            row["reference_comparison"] = _compare_samples(ref_snapshot, snapshot)
            if not row["reference_comparison"]["safe"]:
                issues.append("model comparison with the 3080 reference exceeds existing limits")
    else:
        errors.append("a complete 3080 reference is required for cross-PC comparison")
        for profile in comparable:
            reports[profile]["errors"].append("cannot compare without the 3080 reference")

    for row in reports.values():
        row["valid"] = pin is not None and not row["errors"] and baseline is not None
    return {
        "schema_version": 1,
        "valid": not errors and all(row["valid"] for row in reports.values()),
        "read_only": True,
        "expected_commit": pin,
        "reference_profile": REFERENCE_PROFILE,
        "filter_signature": reference.get("filter_signature") if reference else None,
        "baseline_path": str(baseline_path),
        "profiles": reports,
        "errors": errors,
    }
