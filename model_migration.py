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
from metrics import compare_profiles
from model_regression import compare_model_payloads, compare_model_snapshots
from progress import ProgressTracker
from signatures import build_filter_signature

REQUIRED_PROFILES = ("3080", "4090", "5090")
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
    evaluation: dict | None = None,
    filters: dict | None = None,
    metrics: dict | None = None,
    dependencies: dict | None = None,
) -> dict:
    """Combine every required migration artifact into one strict decision."""
    baseline = Path(baseline_path)
    candidates = {
        profile: Path(path)
        for profile, path in (candidate_paths or default_candidate_paths()).items()
    }
    evaluation = evaluation or evaluation_status()
    if filters is None:
        signature = build_filter_signature()
        filters = ProgressTracker().get_filter_signature_summary(signature)
    metrics = metrics or compare_profiles()
    dependencies = dependencies or validate_dependency_profiles()
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
    measured = set((metrics.get("profiles") or {}).keys())
    missing_metrics = sorted(set(REQUIRED_PROFILES) - measured)
    requirement(
        "real_workload_benchmarks",
        not missing_metrics,
        "missing=" + (", ".join(missing_metrics) if missing_metrics else "none"),
        "Run the identical real-source benchmark on every GPU profile.",
    )

    comparisons = {}
    candidate_libraries = {}
    baseline_payload = json.loads(baseline.read_text(encoding="utf-8")) if baseline.exists() else None
    for profile in REQUIRED_PROFILES:
        candidate = candidates.get(profile)
        portable = None
        if candidate is None or not candidate.exists():
            try:
                from evidence_bundle import load_profile_evidence

                portable = load_profile_evidence(profile)
            except (OSError, ValueError, json.JSONDecodeError):
                portable = None
        portable_snapshot = (portable or {}).get("model_snapshot")
        if (
            (
                (candidate is None or not candidate.exists())
                and portable_snapshot is None
            )
            or not baseline.exists()
        ):
            comparisons[profile] = {
                "present": bool((candidate and candidate.exists()) or portable_snapshot),
                "safe": False,
                "path": str(candidate) if candidate else None,
            }
            continue
        snapshot = (
            json.loads(candidate.read_text(encoding="utf-8"))
            if candidate is not None and candidate.exists()
            else portable_snapshot
        )
        candidate_libraries[profile] = snapshot.get("libraries") or {}
        if candidate is not None and candidate.exists():
            comparison = compare_model_snapshots(baseline, candidate)
            path_label = str(candidate)
            digest = _sha256(candidate)
        else:
            comparison = compare_model_payloads(
                baseline_payload,
                snapshot,
                baseline_label=str(baseline),
                candidate_label=f"portable profile evidence: {profile}",
            )
            path_label = "portable profile evidence"
            digest = portable.get("content_sha256")
        comparisons[profile] = {
            "present": True,
            "safe": bool(comparison["safe"]),
            "path": path_label,
            "sha256": digest,
            "portable": candidate is None or not candidate.exists(),
            "libraries": snapshot.get("libraries") or {},
            "metrics": comparison["metrics"],
        }
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
