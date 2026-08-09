"""Consolidated project, checkpoint, evaluation, and workstation health report."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from audit import build_audit_plan
from checkpoint import verify_output_integrity
from config import (
    DB_ARCHIVE_PATH,
    PROJECT_ROOT,
    RUN_LOCK_PATH,
    STORIES_DIR,
    get_hardware_profile,
)
from database_checkpoint import database_sync_status
from dependency_profiles import installed_dependency_status, validate_dependency_profiles
from evaluation import evaluation_campaign, evaluation_plan, evaluation_status
from metrics import compare_profiles
from model_migration import build_model_migration_plan
from progress import ProgressTracker
from signatures import build_filter_signature


def _git(*arguments: str, root: Path = PROJECT_ROOT) -> str:
    process = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if process.returncode != 0:
        return ""
    return process.stdout.strip()


def git_health(root: str | Path = PROJECT_ROOT) -> dict:
    project_root = Path(root)
    branch = _git("branch", "--show-current", root=project_root) or "detached"
    commit = _git("rev-parse", "HEAD", root=project_root) or "unknown"
    upstream = _git("rev-parse", "--abbrev-ref", "@{upstream}", root=project_root)
    dirty_paths = [
        line for line in _git("status", "--porcelain", root=project_root).splitlines() if line
    ]
    ahead = behind = None
    if upstream:
        counts = _git(
            "rev-list",
            "--left-right",
            "--count",
            "HEAD...@{upstream}",
            root=project_root,
        ).split()
        if len(counts) == 2:
            ahead, behind = map(int, counts)
    checkpoint_commit = _git(
        "log",
        "-1",
        "--format=%cI",
        "--",
        str(DB_ARCHIVE_PATH.relative_to(PROJECT_ROOT)),
        root=project_root,
    )
    checkpoint_age_hours = None
    if checkpoint_commit:
        checkpoint_time = datetime.fromisoformat(checkpoint_commit)
        checkpoint_age_hours = round(
            (datetime.now(timezone.utc) - checkpoint_time).total_seconds() / 3600,
            2,
        )
    return {
        "branch": branch,
        "commit": commit,
        "upstream": upstream or None,
        "ahead": ahead,
        "behind": behind,
        "dirty": bool(dirty_paths),
        "changed_paths": dirty_paths[:20],
        "checkpoint_commit_at": checkpoint_commit or None,
        "checkpoint_age_hours": checkpoint_age_hours,
    }


def runtime_health(profile_name: str) -> dict:
    profile = get_hardware_profile(profile_name)
    errors = []
    result = {
        "profile": profile.name,
        "workers": profile.workers,
        "candidate_batch_size": profile.candidate_batch_size,
        "inference_batch_size": profile.inference_batch_size,
        "encoding_batch_size": profile.encoding_batch_size,
        "precision": profile.precision,
    }
    try:
        import torch

        cuda_available = bool(torch.cuda.is_available())
        runtime = getattr(torch.version, "cuda", None)
        gpu_name = torch.cuda.get_device_name(0) if cuda_available else None
        capability = torch.cuda.get_device_capability(0) if cuda_available else None
        result.update(
            {
                "torch": torch.__version__,
                "cuda_available": cuda_available,
                "cuda_runtime": runtime,
                "gpu": gpu_name,
                "capability": list(capability) if capability else None,
            }
        )
        if not cuda_available:
            errors.append(f"the {profile.name} profile requires CUDA")
        elif profile.name not in str(gpu_name):
            errors.append(f"the {profile.name} profile selected a different GPU: {gpu_name}")
        if profile.name in {"3080", "4090"} and runtime != "12.1":
            errors.append(f"{profile.name} requires the tracked CUDA 12.1 runtime")
        if profile.name == "5090":
            try:
                version = tuple(int(part) for part in str(runtime).split(".")[:2])
            except (TypeError, ValueError):
                version = ()
            if version < (12, 8):
                errors.append("5090 requires a CUDA 12.8+ runtime")
    except ImportError:
        result["torch"] = None
        errors.append("PyTorch is not installed")
    result["valid"] = not errors
    result["errors"] = errors
    return result


def build_health_checks(payload: dict, full: bool = False) -> list[dict]:
    checks = []

    def add(name: str, status: str, summary: str, action: str | None = None) -> None:
        row = {"name": name, "status": status, "summary": summary}
        if action:
            row["action"] = action
        checks.append(row)

    git = payload["git"]
    add(
        "git_worktree",
        "warning" if git["dirty"] else "pass",
        "worktree has local changes" if git["dirty"] else "worktree is clean",
        "Commit or resolve local changes before a workstation handoff" if git["dirty"] else None,
    )
    add(
        "git_upstream",
        "fail" if (git.get("behind") or 0) > 0 else "pass",
        f"ahead={git.get('ahead')}, behind={git.get('behind')}",
        "Pull the matching remote branch before continuing" if (git.get("behind") or 0) > 0 else None,
    )
    add(
        "crawler_lock",
        "fail" if payload["crawler_lock_exists"] else "pass",
        "crawler lock exists" if payload["crawler_lock_exists"] else "crawler is stopped",
        "Stop the crawler cleanly before maintenance" if payload["crawler_lock_exists"] else None,
    )
    runtime = payload["runtime"]
    add(
        "runtime_profile",
        "pass" if runtime["valid"] else "fail",
        (
            f"{runtime.get('gpu') or 'no GPU'} with CUDA {runtime.get('cuda_runtime') or 'none'}"
            if runtime["valid"]
            else "; ".join(runtime["errors"])
        ),
        "Run scripts/setup.ps1 with the profile for this workstation" if not runtime["valid"] else None,
    )
    database = payload["database"]
    add(
        "database_checkpoint",
        "pass" if database["synchronized"] else "fail",
        "working database matches the shared archive" if database["synchronized"] else "working database and archive differ",
        "Create or restore a verified checkpoint" if not database["synchronized"] else None,
    )
    progress = payload["progress"]
    add(
        "active_sources",
        "fail" if int(progress.get("processing", 0)) else "pass",
        f"{progress.get('processing', 0)} source(s) marked processing",
        "Recover or finish active source leases" if int(progress.get("processing", 0)) else None,
    )
    dependencies = payload["dependencies"]
    dependency_valid = dependencies["profiles"]["valid"] and dependencies["installed"]["valid"]
    add(
        "dependency_profiles",
        "pass" if dependency_valid else "fail",
        "tracked and installed profile dependencies agree" if dependency_valid else "dependency profile mismatch",
        "Run scripts/setup.ps1 for this GPU profile" if not dependency_valid else None,
    )
    policy = dependencies["profiles"].get("security_policy", {})
    add(
        "dependency_security",
        "warning" if policy.get("status") == "migration_required" else "pass",
        (
            f"model-stack migration due by {policy.get('review_by')}"
            if policy.get("status") == "migration_required"
            else "no temporary dependency exception"
        ),
        "Complete model regression and three-GPU validation" if policy.get("status") == "migration_required" else None,
    )
    evaluation = payload["evaluation"]
    add(
        "evaluation_baseline",
        "pass" if evaluation["baseline"]["ready"] else "warning",
        f"{evaluation['labeled']}/{evaluation['baseline']['minimum_labels']} required labels",
        "Run python main.py evaluation plan" if not evaluation["baseline"]["ready"] else None,
    )
    filters = payload["filters"]
    add(
        "filter_signatures",
        "pass" if int(filters["current"]) else "warning",
        f"current={filters['current']}, unknown={filters['unknown']}, stale={filters['stale']}",
        "Run a bounded audit before adopting or recrawling historical work" if not int(filters["current"]) else None,
    )
    measured = set(payload["metrics"].get("profiles", {}))
    missing_profiles = sorted({"3080", "4090", "5090"} - measured)
    add(
        "hardware_metrics",
        "warning" if missing_profiles else "pass",
        "missing real metrics for: " + ", ".join(missing_profiles) if missing_profiles else "all GPU profiles measured",
        "Run identical real-source benchmarks on each workstation" if missing_profiles else None,
    )
    add(
        "model_baseline",
        "pass" if payload["model_baseline_exists"] else "warning",
        "model regression baseline exists" if payload["model_baseline_exists"] else "model regression baseline is missing",
        "Run python main.py model-validation capture --profile PROFILE" if not payload["model_baseline_exists"] else None,
    )
    migration = payload["model_migration"]
    add(
        "model_migration_gate",
        "pass" if migration["ready"] else "warning",
        (
            "all migration evidence is complete"
            if migration["ready"]
            else f"{len(migration['blockers'])} migration requirement(s) blocked"
        ),
        "Run scripts/model-migration.ps1 -Action plan" if not migration["ready"] else None,
    )
    if full:
        output = payload["output"]
        add(
            "output_integrity",
            "pass" if output["valid"] else "fail",
            f"{output['integrity_errors']} integrity error(s)",
            "Run output verification and restore damaged shards" if not output["valid"] else None,
        )
        stories = payload["stories"]
        add(
            "story_integrity",
            "pass" if stories["valid"] else "fail",
            f"{stories['integrity_errors']} integrity error(s)",
            "Rebuild and verify a stopped story checkpoint" if not stories["valid"] else None,
        )
        story_packs = payload["story_packs"]
        add(
            "story_pack_integrity",
            "pass" if story_packs["valid"] else "fail",
            (
                f"{story_packs.get('fragments', 0)} fragment(s) in "
                f"{story_packs.get('packs', 0)} synchronized pack(s)"
            ),
            (
                "Rebuild deterministic story packs from verified local fragments"
                if not story_packs["valid"]
                else None
            ),
        )
    return checks


def collect_story_storage_health(stories_dir: str | Path = STORIES_DIR) -> tuple[dict, dict]:
    """Verify local fragments, or accept verified packs as restorable storage."""
    root = Path(stories_dir)
    from story_packing import verify_story_packs
    from story_products import verify_story_integrity

    packs = verify_story_packs(root)
    if any((root / "_records").glob("*.jsonl.gz")):
        stories = verify_story_integrity(root)
    elif packs["valid"]:
        stories = {
            "valid": True,
            "integrity_errors": 0,
            "restorable_from_packs": True,
        }
    else:
        stories = verify_story_integrity(root)
    return stories, packs


def collect_project_health(profile_name: str = "auto", full: bool = False) -> dict:
    profile = get_hardware_profile(profile_name)
    tracker = ProgressTracker()
    signature = build_filter_signature()
    filters = tracker.get_filter_signature_summary(signature)
    plan = build_audit_plan(signature, per_crawl=2, tracker=tracker)
    evaluation = evaluation_status()
    metrics = compare_profiles()
    dependency_profiles = validate_dependency_profiles()
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": git_health(),
        "crawler_lock_exists": RUN_LOCK_PATH.exists(),
        "runtime": runtime_health(profile.name),
        "database": database_sync_status(),
        "progress": tracker.get_summary(),
        "filters": filters,
        "evaluation": evaluation,
        "evaluation_plan": evaluation_plan(),
        "evaluation_campaign": evaluation_campaign(include_samples=False),
        "audit_readiness": {
            "selected_sources": plan["total_sources"],
            "sources_by_crawl": plan["sources_by_crawl"],
            "command": "python main.py audit plan --per-crawl 2",
        },
        "dependencies": {
            "profiles": dependency_profiles,
            "installed": installed_dependency_status(profile.name),
        },
        "metrics": metrics,
        "model_baseline_exists": (PROJECT_ROOT / "data/evaluation/model-baseline.json").exists(),
    }
    payload["model_migration"] = build_model_migration_plan(
        evaluation=evaluation,
        filters=filters,
        metrics=metrics,
        dependencies=dependency_profiles,
    )
    if full:
        payload["output"] = verify_output_integrity()
        payload["stories"], payload["story_packs"] = collect_story_storage_health()
    checks = build_health_checks(payload, full=full)
    payload["checks"] = checks
    payload["status"] = (
        "fail"
        if any(check["status"] == "fail" for check in checks)
        else "attention"
        if any(check["status"] == "warning" for check in checks)
        else "healthy"
    )
    payload["actions"] = [
        check["action"] for check in checks if check.get("action")
    ]
    return payload


def main() -> int:
    print(json.dumps(collect_project_health(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
