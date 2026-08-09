"""Bounded, evidence-gated replay of operational source failures."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from audit import run_audit
from config import AUDIT_DIR, RECOVERY_EVIDENCE_DIR
from failure_analysis import classify_failure
from progress import ProgressTracker
from signatures import build_filter_signature


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def build_recovery_plan(
    categories: list[str] | None = None,
    per_category: int = 5,
    *,
    tracker: ProgressTracker | None = None,
    filter_signature: str | None = None,
) -> dict:
    tracker = tracker or ProgressTracker()
    selected = tracker.select_failed_sources(categories, per_category)
    counts = Counter(str(row["failure_category"]) for row in selected)
    sources = [
        {
            **row,
            "historical_matches": 0,
            "completed_at": row.get("failed_at"),
            "signature_state": "failed",
            "filter_signature": "",
        }
        for row in selected
    ]
    selection_digest = hashlib.sha256(_canonical(sources)).hexdigest()
    return {
        "schema_version": 1,
        "campaign_type": "failure_recovery",
        "created_at": _utc_now(),
        "filter_signature": filter_signature or build_filter_signature(),
        "selection": {
            "per_crawl": per_category,
            "per_category": per_category,
            "requested_categories": sorted(set(categories or [])),
            "deterministic": True,
            "selection_sha256": selection_digest,
        },
        "total_sources": len(sources),
        "sources_by_category": dict(sorted(counts.items())),
        "preserves_historical_state": True,
        "sources": sources,
    }


def run_recovery_campaign(
    plan: dict,
    settings,
    *,
    audit_dir: str | Path = AUDIT_DIR / "recovery-campaigns",
    context=None,
    shutdown_event=None,
) -> dict:
    if plan.get("campaign_type") != "failure_recovery":
        raise ValueError("not a failure recovery plan")
    expected = (plan.get("selection") or {}).get("selection_sha256")
    if expected != hashlib.sha256(_canonical(plan.get("sources") or [])).hexdigest():
        raise ValueError("recovery plan selection digest is invalid")
    report = run_audit(
        plan,
        settings,
        audit_dir=audit_dir,
        sample_rate=0.0,
        context=context,
        shutdown_event=shutdown_event,
    )
    source_results = []
    category_counts: dict[str, Counter] = {}
    for row in report.get("sources", []):
        category = str(row.get("failure_category", "other"))
        recovered = row.get("audit_status") == "completed"
        counter = category_counts.setdefault(category, Counter())
        counter["attempted"] += 1
        counter["recovered" if recovered else "failed"] += 1
        source_results.append(
            {
                "file_path": row.get("file_path"),
                "crawl_id": row.get("crawl_id"),
                "original_category": category,
                "original_error_sha256": row.get("error_sha256"),
                "recovered": recovered,
                "replay_status": row.get("audit_status"),
                "replay_failure_category": (
                    None if recovered else classify_failure(row.get("audit_error"))
                ),
            }
        )
    by_category = {
        category: {
            **dict(values),
            "recovery_rate": round(values["recovered"] / values["attempted"], 6),
        }
        for category, values in sorted(category_counts.items())
    }
    result = {
        "schema_version": 1,
        "campaign_type": "failure_recovery",
        "campaign_id": report["audit_id"],
        "created_at": _utc_now(),
        "filter_signature": plan["filter_signature"],
        "selection_sha256": expected,
        "historical_state_changed": False,
        "attempted_sources": len(source_results),
        "recovered_sources": sum(row["recovered"] for row in source_results),
        "by_category": by_category,
        "sources": source_results,
        "audit_report": str(Path(report["audit_root"]) / "report.json"),
    }
    target = Path(report["audit_root"]) / "recovery-report.json"
    _atomic_json(target, result)
    result["report_path"] = str(target)
    return result


def recovery_evidence_status(
    report_path: str | Path,
    *,
    tracker: ProgressTracker | None = None,
    current_signature: str | None = None,
) -> dict:
    path = Path(report_path)
    raw = path.read_bytes()
    report = json.loads(raw.decode("utf-8"))
    if report.get("campaign_type") != "failure_recovery":
        raise ValueError("not a failure recovery report")
    signature = current_signature or build_filter_signature()
    if report.get("filter_signature") != signature:
        raise ValueError("recovery report filter signature is stale")
    if report.get("historical_state_changed") is not False:
        raise ValueError("recovery report does not preserve historical state")
    recovered = [row for row in report.get("sources", []) if row.get("recovered")]
    if not recovered:
        raise ValueError("recovery report contains no verified recovered sources")
    tracker = tracker or ProgressTracker()
    current = {
        row["file_path"]: row
        for row in tracker.select_failed_sources(per_category=1_000_000)
    }
    eligible = []
    for row in recovered:
        source = current.get(str(row.get("file_path", "")))
        if source is None:
            continue
        if source.get("failure_category") != row.get("original_category"):
            continue
        if source.get("error_sha256") != row.get("original_error_sha256"):
            continue
        eligible.append(str(row["file_path"]))
    if not eligible:
        raise ValueError("no recovered source still matches the current failure state")
    return {
        "schema_version": 1,
        "valid": True,
        "report_path": str(path.resolve()),
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "campaign_id": report.get("campaign_id"),
        "eligible_sources": sorted(eligible),
        "requires_confirmation": True,
    }


def adopt_recovery_evidence(
    report_path: str | Path,
    *,
    tracker: ProgressTracker | None = None,
    target_dir: str | Path = RECOVERY_EVIDENCE_DIR,
) -> dict:
    tracker = tracker or ProgressTracker()
    status = recovery_evidence_status(report_path, tracker=tracker)
    source = Path(report_path)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (
        f"{status['campaign_id']}-{status['report_sha256'][:12]}.json"
    )
    if target.exists() and target.read_bytes() != source.read_bytes():
        raise ValueError("archived recovery evidence conflicts with this report")
    if not target.exists():
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    reset = tracker.retry_failed_paths(status["eligible_sources"])
    return {
        **status,
        "adopted": True,
        "rows_reset": reset,
        "archived_report": str(target),
    }
