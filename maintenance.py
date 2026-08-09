"""Read-only operational plan for crawl, evaluation, and model-stack work."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from audit import build_audit_plan
from config import MODEL_BASELINE_PATH, get_hardware_profile
from dependency_profiles import validate_dependency_profiles
from evaluation import evaluation_plan
from metrics import compare_profiles
from progress import ProgressTracker
from signatures import build_filter_signature

REQUIRED_GPU_PROFILES = ("3080", "4090", "5090")


def _failure_waves(failures: dict) -> list[dict]:
    waves = []
    for category, details in failures.get("categories", {}).items():
        count = int(details.get("count", 0))
        if not count:
            continue
        limit = min(count, 25 if category.startswith("http_") else 64)
        waves.append(
            {
                "category": category,
                "sources": count,
                "bounded_reset_limit": limit,
                "reset_command": (
                    ".\\scripts\\retry.ps1 -All "
                    f"-Category {category} -Limit {limit} -Apply"
                ),
                "requires_review": True,
            }
        )
    return waves


def assemble_maintenance_plan(
    *,
    profile_name: str,
    progress: dict,
    failures: dict,
    evaluation: dict,
    filters: dict,
    audit: dict,
    dependencies: dict,
    metrics: dict,
    model_baseline_exists: bool,
) -> dict:
    """Combine existing evidence into ordered actions without changing state."""
    total = int(progress.get("total_files", 0))
    completed = int(progress.get("completed", 0))
    pending = int(progress.get("pending", 0))
    retryable = int(failures.get("retryable_now", 0))
    measured_profiles = sorted(set(metrics.get("profiles", {})))
    missing_profiles = sorted(set(REQUIRED_GPU_PROFILES) - set(measured_profiles))
    filters_ready = (
        int(filters.get("completed", 0)) > 0
        and int(filters.get("unknown", 0)) == 0
        and int(filters.get("stale", 0)) == 0
    )
    evaluation_ready = bool(evaluation.get("ready"))
    policy = dependencies.get("security_policy", {})
    migration_required = policy.get("status") == "migration_required"
    migration_blockers = []
    if not model_baseline_exists:
        migration_blockers.append("tracked model-regression baseline is missing")
    if not evaluation_ready:
        migration_blockers.append("human evaluation and holdout minimums are incomplete")
    if not filters_ready:
        migration_blockers.append("historical filter signatures are not fully audited")
    if missing_profiles:
        migration_blockers.append(
            "real-source metrics are missing for " + ", ".join(missing_profiles)
        )

    actions = []
    if int(progress.get("processing", 0)):
        actions.append(
            {
                "priority": 1,
                "area": "crawl",
                "action": "Recover or finish active leases before maintenance.",
                "command": "python main.py recover",
            }
        )
    elif pending or retryable:
        actions.append(
            {
                "priority": 1,
                "area": "crawl",
                "action": "Resume the crawl; retryable failures are consumed automatically.",
                "command": (
                    f".\\scripts\\run.ps1 -Profile {profile_name} "
                    "run --all --strategy yield-aware --chunk-size 100"
                ),
            }
        )
    if not filters_ready:
        actions.append(
            {
                "priority": 2,
                "area": "filters",
                "action": "Run an isolated historical audit before adopting signatures.",
                "command": (
                    f".\\scripts\\audit.ps1 -Action run -Profile {profile_name} "
                    "-PerCrawl 5 -Apply"
                ),
            }
        )
    if not evaluation_ready:
        actions.append(
            {
                "priority": 3,
                "area": "evaluation",
                "action": "Collect the remaining human labels; do not auto-label samples.",
                "command": evaluation.get(
                    "browser_command",
                    "python main.py evaluation serve --open-browser",
                ),
            }
        )
    for missing in missing_profiles:
        actions.append(
            {
                "priority": 4,
                "area": "hardware",
                "action": f"Capture an identical real-source benchmark on the {missing} PC.",
                "command": (
                    f".\\scripts\\benchmark.ps1 -Profile {missing} -Real "
                    "-Sources 5 -WorkerCount 1,7 -Apply"
                ),
            }
        )
    if migration_required:
        actions.append(
            {
                "priority": 5,
                "area": "dependencies",
                "action": (
                    "Migrate the model stack only after every validation blocker is cleared."
                ),
                "command": (
                    ".\\scripts\\model-migration.ps1 -Action plan"
                ),
            }
        )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "profile": profile_name,
        "status": "action_required" if actions else "ready",
        "crawl": {
            "total_sources": total,
            "completed_sources": completed,
            "pending_sources": pending,
            "completion_pct": round(completed / total * 100, 2) if total else 0.0,
            "failed_sources": int(failures.get("failed", 0)),
            "retryable_now": retryable,
            "attempts_exhausted": int(failures.get("attempts_exhausted", 0)),
            "failure_waves": _failure_waves(failures),
            "retry_policy": (
                "Normal crawl runs consume retryable failures. Use a bounded reset only "
                "after reviewing the category and underlying cause."
            ),
        },
        "filters": {
            **filters,
            "ready": filters_ready,
            "audit_sources": int(audit.get("total_sources", 0)),
            "audit_command": "python main.py audit plan --per-crawl 5",
        },
        "evaluation": {
            "ready": evaluation_ready,
            "requires_human_judgment": True,
            "automatic_labeling_allowed": False,
            "remaining_to_target": int(evaluation.get("remaining_to_target", 0)),
            "steps": evaluation.get("steps", []),
            "insufficient_queues": evaluation.get("insufficient_queues", []),
            "browser_command": evaluation.get("browser_command"),
        },
        "model_migration": {
            "required": migration_required,
            "review_by": policy.get("review_by"),
            "days_until_review": policy.get("days_until_review"),
            "tracked_baseline": model_baseline_exists,
            "measured_profiles": measured_profiles,
            "missing_profiles": missing_profiles,
            "ready": migration_required and not migration_blockers,
            "blockers": migration_blockers,
            "claim_policy": (
                "Do not claim migration readiness without human labels, an audited "
                "filter state, and real measurements from all three GPU profiles."
            ),
        },
        "actions": sorted(actions, key=lambda row: (row["priority"], row["area"])),
    }


def collect_maintenance_plan(profile_name: str = "auto") -> dict:
    profile = get_hardware_profile(profile_name)
    tracker = ProgressTracker()
    signature = build_filter_signature()
    return assemble_maintenance_plan(
        profile_name=profile.name,
        progress=tracker.get_summary(),
        failures=tracker.get_failure_summary(examples_per_category=0),
        evaluation=evaluation_plan(),
        filters=tracker.get_filter_signature_summary(signature),
        audit=build_audit_plan(signature, per_crawl=5, tracker=tracker),
        dependencies=validate_dependency_profiles(),
        metrics=compare_profiles(),
        model_baseline_exists=Path(MODEL_BASELINE_PATH).exists(),
    )
