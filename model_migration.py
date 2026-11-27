"""Evidence gate for coordinated semantic-model dependency migrations."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR, MODEL_BASELINE_PATH
from dependency_profiles import validate_dependency_profiles
from evaluation import evaluation_status
from gpu_evidence import REQUIRED_PROFILES, compare_gpu_evidence
from model_regression import compare_model_snapshots
from progress import ProgressTracker
from signatures import build_filter_signature

DEFAULT_EVIDENCE_PATH = DATA_DIR / "checkpoints" / "model-migration-evidence.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def default_candidate_paths() -> dict[str, Path]:
    return {
        profile: DATA_DIR / "evaluation" / f"model-candidate-{profile}.json"
        for profile in REQUIRED_PROFILES
    }


def build_model_migration_plan(
    *,
    baseline_path: str | Path = MODEL_BASELINE_PATH,
    candidate_paths: dict[str, str | Path] | None = None,
    expected_commit: str | None = None,
    evidence_paths: dict[str, str | Path] | None = None,
    evaluation: dict | None = None,
    filters: dict | None = None,
    dependencies: dict | None = None,
) -> dict:
    """Combine every required migration artifact into one strict decision."""
    baseline = Path(baseline_path)
    candidates = {
        profile: Path(path)
        for profile, path in (candidate_paths or default_candidate_paths()).items()
    }
    evaluation = evaluation or evaluation_status()
    signature = build_filter_signature()
    if filters is None:
        filters = ProgressTracker().get_filter_signature_summary(signature)
    dependencies = dependencies or validate_dependency_profiles()
    evidence = compare_gpu_evidence(expected_commit, evidence_paths, baseline_path=baseline)
    requirements = []

    def requirement(name: str, passed: bool, detail: str, action: str) -> None:
        requirements.append(
            {
                "name": name,
                "passed": bool(passed),
                "detail": detail,
                "action": None if passed else action,
            }
        )

    requirement(
        "baseline_snapshot",
        baseline.exists(),
        str(baseline),
        "Capture the tracked model baseline before changing the environment.",
    )
    evaluation_ready = bool((evaluation.get("baseline") or {}).get("ready"))
    holdout_ready = bool((evaluation.get("baseline") or {}).get("holdout_ready"))
    requirement(
        "human_evaluation",
        evaluation_ready and holdout_ready,
        (
            f"labels={evaluation.get('labeled', 0)}, "
            f"holdout={int((evaluation.get('baseline') or {}).get('holdout_labels', 0))}"
        ),
        "Complete the guided human evaluation campaign.",
    )
    filter_ready = (
        int(filters.get("completed", 0)) > 0
        and int(filters.get("unknown", 0)) == 0
        and int(filters.get("stale", 0)) == 0
    )
    requirement(
        "historical_filter_evidence",
        filter_ready,
        (
            f"current={filters.get('current', 0)}, "
            f"unknown={filters.get('unknown', 0)}, stale={filters.get('stale', 0)}"
        ),
        "Run the isolated audit and adopt only validated crawl signatures.",
    )
    evidence_errors = list(evidence["errors"])
    invalid_profiles = [profile for profile, row in evidence["profiles"].items() if not row["valid"]]
    if invalid_profiles:
        evidence_errors.append(f"unverified profiles: {', '.join(invalid_profiles)}")
    if evidence["filter_signature"] and evidence["filter_signature"] != signature:
        evidence_errors.append("evidence does not match this checkout's filter signature")
    requirement(
        "real_workload_benchmarks",
        evidence["valid"] and not evidence_errors,
        "; ".join(evidence_errors) or "all three pinned model and benchmark bundles agree",
        "Run evidence compare with the intended runtime commit and the three profile bundles.",
    )

    comparisons = {}
    candidate_libraries = {}
    for profile in REQUIRED_PROFILES:
        bundle = evidence["profiles"][profile]
        comparison = bundle.get("baseline_comparison")
        if comparison is not None:
            candidate_libraries[profile] = bundle["libraries"]
            comparisons[profile] = {
                "present": True,
                "safe": comparison["safe"],
                "path": bundle["path"],
                "sha256": bundle["content_sha256"],
                "portable": True,
                "libraries": bundle["libraries"],
                "metrics": comparison["metrics"],
            }
            continue

        # Raw captures remain useful diagnostics, but cannot satisfy the bundle requirement.
        candidate = candidates.get(profile)
        row = {
            "present": bool(candidate and candidate.is_file()),
            "safe": False,
            "path": str(candidate) if candidate else None,
            "portable": False,
        }
        comparisons[profile] = row
        if candidate is not None and row["present"] and baseline.is_file():
            try:
                comparison = compare_model_snapshots(baseline, candidate)
                row.update(
                    safe=comparison["safe"],
                    sha256=_sha256(candidate),
                    libraries=comparison["candidate"]["libraries"],
                    metrics=comparison["metrics"],
                )
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                row["error"] = f"cannot compare raw snapshot: {exc}"
    missing_candidates = [
        profile for profile in REQUIRED_PROFILES if not comparisons[profile]["present"]
    ]
    unsafe_candidates = [
        profile
        for profile in REQUIRED_PROFILES
        if comparisons[profile]["present"] and not comparisons[profile]["safe"]
    ]
    consistent_libraries = (
        len(candidate_libraries) == len(REQUIRED_PROFILES)
        and len({json.dumps(value, sort_keys=True) for value in candidate_libraries.values()}) == 1
    )
    requirement(
        "candidate_snapshots",
        not missing_candidates and not unsafe_candidates,
        (f"missing={missing_candidates or 'none'}, unsafe={unsafe_candidates or 'none'}"),
        "Capture and compare one candidate snapshot on each workstation.",
    )
    requirement(
        "candidate_library_consistency",
        consistent_libraries,
        "candidate environments agree" if consistent_libraries else "candidate locks differ",
        "Install the same proposed model stack on all three workstations.",
    )
    profiles_valid = bool(dependencies.get("valid"))
    requirement(
        "dependency_profile_contract",
        profiles_valid,
        "tracked dependency profiles valid" if profiles_valid else "profile contract invalid",
        "Resolve dependency profile errors before updating shared locks.",
    )
    ready = all(item["passed"] for item in requirements)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ready": ready,
        "status": "ready_for_lock_update" if ready else "blocked",
        "baseline": {
            "path": str(baseline),
            "present": baseline.exists(),
            "sha256": _sha256(baseline) if baseline.exists() else None,
        },
        "comparisons": comparisons,
        "gpu_evidence": evidence,
        "requirements": requirements,
        "blockers": [item for item in requirements if not item["passed"]],
        "approval_policy": (
            "Shared dependency locks may change only when every requirement passes."
        ),
    }


def approve_model_migration(
    plan: dict,
    output_path: str | Path = DEFAULT_EVIDENCE_PATH,
) -> dict:
    """Write portable approval evidence only for a fully satisfied plan."""
    if not plan.get("ready") or plan.get("blockers"):
        raise ValueError("model migration is blocked; no approval evidence was written")
    payload = {
        **plan,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "approved": True,
        "generated_text": False,
    }
    target = Path(output_path)
    _atomic_json(target, payload)
    return {
        "approved": True,
        "path": str(target),
        "sha256": _sha256(target),
    }
