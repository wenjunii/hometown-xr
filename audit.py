"""Isolated, deterministic audits of completed Common Crawl sources."""

from __future__ import annotations

import gzip
import hashlib
import json
import multiprocessing
import os
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from config import (
    AUDIT_DEFAULT_PER_CRAWL,
    AUDIT_DIR,
    AUDIT_EVIDENCE_DIR,
    AUDIT_MIN_ADOPTION_SOURCES,
    AUDIT_SAMPLE_RATE,
    EVALUATION_DIR,
    METRICS_DIR,
    OUTPUT_DIR,
)
from crawl_catalog import get_crawl_info
from evaluation import DecisionSampler
from metrics import MetricsRecorder
from output import OutputWriter
from pipeline import ExtractionPipeline, InferenceService
from progress import ProgressTracker
from record_identity import content_fingerprint
from signatures import build_run_manifest
from text_normalization import normalize_extracted_text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gpu_name() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except (ImportError, RuntimeError):
        pass
    return "CPU"


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def build_audit_plan(
    current_signature: str,
    per_crawl: int = AUDIT_DEFAULT_PER_CRAWL,
    crawl_ids: list[str] | None = None,
    include_current: bool = False,
    tracker: ProgressTracker | None = None,
) -> dict:
    """Build a read-only, deterministic sample of completed sources."""
    tracker = tracker or ProgressTracker()
    sources = tracker.sample_completed_for_audit(
        current_signature,
        per_crawl,
        crawl_ids=crawl_ids,
        include_current=include_current,
    )
    by_crawl = Counter(str(row["crawl_id"]) for row in sources)
    by_signature = Counter(str(row["signature_state"]) for row in sources)
    return {
        "schema_version": 1,
        "created_at": _utc_now(),
        "filter_signature": current_signature,
        "selection": {
            "per_crawl": per_crawl,
            "requested_crawls": sorted(set(crawl_ids or [])),
            "include_current": include_current,
            "deterministic": True,
        },
        "total_sources": len(sources),
        "sources_by_crawl": dict(sorted(by_crawl.items())),
        "sources_by_signature_state": dict(sorted(by_signature.items())),
        "preserves_historical_state": True,
        "sources": sources,
    }


def _read_source_records(writer: OutputWriter, source_file: str) -> list[dict]:
    records = []
    for path in writer.find_source_outputs(source_file):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            records.extend(json.loads(line) for line in handle if line.strip())
    return records


def _comparison_counter(records: list[dict]) -> Counter:
    return Counter(
        content_fingerprint(
            str(record.get("url", "")),
            normalize_extracted_text(str(record.get("paragraph", ""))),
        )
        for record in records
    )


def output_match_set_digest(
    output_dir: str | Path,
    source_files: list[str],
) -> str:
    """Hash the normalized output multiset for repeatability comparisons."""
    writer = OutputWriter(output_dir)
    values = Counter()
    for source_file in sorted(source_files):
        values.update(_comparison_counter(_read_source_records(writer, source_file)))
    payload = json.dumps(
        sorted(values.items()),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compare_audit_outputs(
    plan: dict,
    states: dict[str, dict],
    historical_output: str | Path = OUTPUT_DIR,
    audit_output: str | Path | None = None,
    minimum_adoption_sources: int = AUDIT_MIN_ADOPTION_SOURCES,
) -> dict:
    """Compare isolated results with historical shards using normalized identity."""
    if audit_output is None:
        raise ValueError("audit_output is required")
    if minimum_adoption_sources <= 0:
        raise ValueError("minimum_adoption_sources must be positive")
    historical_writer = OutputWriter(historical_output)
    audit_writer = OutputWriter(audit_output)
    sources = []
    totals = Counter()
    per_crawl: dict[str, Counter] = defaultdict(Counter)

    for selected in plan.get("sources", []):
        source_file = str(selected["file_path"])
        crawl_id = str(selected["crawl_id"])
        state = states.get(source_file, {"status": "missing"})
        row = {
            **selected,
            "audit_status": state.get("status", "missing"),
            "audit_error": state.get("error"),
        }
        totals["selected_sources"] += 1
        per_crawl[crawl_id]["selected_sources"] += 1
        if state.get("status") != "completed":
            sources.append(row)
            continue

        historical = _read_source_records(historical_writer, source_file)
        current = _read_source_records(audit_writer, source_file)
        historical_keys = _comparison_counter(historical)
        current_keys = _comparison_counter(current)
        overlap = sum((historical_keys & current_keys).values())
        removed = sum((historical_keys - current_keys).values())
        added = sum((current_keys - historical_keys).values())
        repaired = sum(bool(record.get("raw_paragraph")) for record in current)
        row.update(
            {
                "historical_output_matches": len(historical),
                "audit_matches": len(current),
                "normalized_overlap": overlap,
                "removed_matches": removed,
                "added_matches": added,
                "normalized_text_repairs": repaired,
            }
        )
        totals.update(
            {
                "completed_sources": 1,
                "historical_matches": len(historical),
                "audit_matches": len(current),
                "normalized_overlap": overlap,
                "removed_matches": removed,
                "added_matches": added,
                "normalized_text_repairs": repaired,
                "sources_with_match_delta": int(added > 0 or removed > 0),
            }
        )
        per_crawl[crawl_id].update(
            {
                "completed_sources": 1,
                "historical_matches": len(historical),
                "audit_matches": len(current),
                "removed_matches": removed,
                "added_matches": added,
            }
        )
        sources.append(row)

    completed = totals["completed_sources"]
    equivalent = (
        completed == totals["selected_sources"]
        and totals["added_matches"] == 0
        and totals["removed_matches"] == 0
    )
    adoption_by_crawl = {}
    for crawl_id, values in sorted(per_crawl.items()):
        selected = int(values["selected_sources"])
        completed_count = int(values["completed_sources"])
        equivalent_crawl = (
            completed_count == selected
            and int(values["added_matches"]) == 0
            and int(values["removed_matches"]) == 0
        )
        eligible = selected >= minimum_adoption_sources and equivalent_crawl
        reasons = []
        if selected < minimum_adoption_sources:
            reasons.append(
                f"requires at least {minimum_adoption_sources} audited sources"
            )
        if completed_count != selected:
            reasons.append("not every selected source completed")
        if int(values["added_matches"]) or int(values["removed_matches"]):
            reasons.append("the normalized match set changed")
        adoption_by_crawl[crawl_id] = {
            "eligible": eligible,
            "selected_sources": selected,
            "completed_sources": completed_count,
            "minimum_sources": minimum_adoption_sources,
            "reasons": reasons,
        }

    return {
        "summary": {
            **dict(totals),
            "equivalent_normalized_match_sets": equivalent,
            "historical_state_changed": False,
        },
        "by_crawl": {
            crawl_id: dict(values) for crawl_id, values in sorted(per_crawl.items())
        },
        "adoption": {
            "eligible_crawls": [
                crawl_id
                for crawl_id, result in adoption_by_crawl.items()
                if result["eligible"]
            ],
            "minimum_sources_per_crawl": minimum_adoption_sources,
            "by_crawl": adoption_by_crawl,
        },
        "sources": sources,
    }


def load_adoption_evidence(
    report_path: str | Path,
    current_signature: str,
    requested_crawls: list[str] | None = None,
) -> dict:
    """Validate a completed audit report before historical signatures are adopted."""
    path = Path(report_path)
    raw = path.read_bytes()
    report = json.loads(raw.decode("utf-8"))
    audit_id = str(report.get("audit_id") or "")
    if not audit_id or audit_id == "unknown":
        raise ValueError("audit report has no valid audit id")
    if report.get("filter_signature") != current_signature:
        raise ValueError("audit report filter signature does not match the current filter")
    if (report.get("summary") or {}).get("historical_state_changed") is not False:
        raise ValueError("audit report does not prove that historical state was preserved")
    adoption = report.get("adoption") or {}
    eligible = set(adoption.get("eligible_crawls") or [])
    requested = set(requested_crawls or eligible)
    if not requested:
        raise ValueError("audit report has no crawls eligible for signature adoption")
    blocked = sorted(requested - eligible)
    if blocked:
        raise ValueError(
            "audit evidence is insufficient for: " + ", ".join(blocked)
        )
    minimum = int(adoption.get("minimum_sources_per_crawl") or 0)
    by_crawl = adoption.get("by_crawl") or {}
    source_rows = report.get("sources") or []
    for crawl_id in requested:
        evidence = by_crawl.get(crawl_id) or {}
        selected = int(evidence.get("selected_sources") or 0)
        completed = int(evidence.get("completed_sources") or 0)
        matching_sources = [
            row for row in source_rows if str(row.get("crawl_id")) == crawl_id
        ]
        source_sets_match = all(
            row.get("audit_status") == "completed"
            and int(row.get("added_matches") or 0) == 0
            and int(row.get("removed_matches") or 0) == 0
            for row in matching_sources
        )
        if (
            minimum <= 0
            or not evidence.get("eligible")
            or selected < minimum
            or completed != selected
            or len(matching_sources) != selected
            or not source_sets_match
        ):
            raise ValueError(f"audit evidence is internally inconsistent for: {crawl_id}")
    return {
        "audit_id": audit_id,
        "report_path": str(path.resolve()),
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "eligible_crawls": sorted(requested),
    }


def archive_adoption_evidence(
    report_path: str | Path,
    evidence: dict,
    target_dir: str | Path = AUDIT_EVIDENCE_DIR,
) -> Path:
    """Copy validated evidence into the Git-tracked cross-PC checkpoint tree."""
    source = Path(report_path)
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != evidence.get("report_sha256"):
        raise ValueError("audit report changed after validation")
    audit_id = str(evidence.get("audit_id") or "")
    safe_audit_id = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in audit_id
    ).strip("_")
    if not safe_audit_id:
        raise ValueError("audit evidence has no archive-safe audit id")
    target_dir = Path(target_dir)
    target = target_dir / f"{safe_audit_id}-{digest[:12]}.json"
    if target.exists():
        if target.read_bytes() != raw:
            raise ValueError(f"archived audit evidence conflicts with {target}")
        return target
    target_dir.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, target)
    return target


def audit_evidence_status(
    report_path: str | Path,
    current_signature: str,
    requested_crawls: list[str] | None = None,
) -> dict:
    """Validate one report and describe the exact signature adoption it permits."""
    evidence = load_adoption_evidence(
        report_path,
        current_signature,
        requested_crawls=requested_crawls,
    )
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    adoption = report.get("adoption") or {}
    selected = set(evidence["eligible_crawls"])
    return {
        "schema_version": 1,
        "valid": True,
        "historical_state_changed": False,
        "current_signature": current_signature,
        "audit_id": evidence["audit_id"],
        "report_path": evidence["report_path"],
        "report_sha256": evidence["report_sha256"],
        "eligible_crawls": evidence["eligible_crawls"],
        "by_crawl": {
            crawl_id: details
            for crawl_id, details in sorted((adoption.get("by_crawl") or {}).items())
            if crawl_id in selected
        },
        "requires_confirmation": True,
        "adopt_command": (
            "python main.py audit adopt --report "
            f'"{evidence["report_path"]}" --yes'
        ),
    }


def adopt_audit_evidence(
    report_path: str | Path,
    current_signature: str,
    requested_crawls: list[str] | None = None,
    *,
    tracker: ProgressTracker | None = None,
    target_dir: str | Path = AUDIT_EVIDENCE_DIR,
) -> dict:
    """Archive validated evidence and atomically stamp only covered crawls."""
    status = audit_evidence_status(
        report_path,
        current_signature,
        requested_crawls=requested_crawls,
    )
    evidence = {
        "audit_id": status["audit_id"],
        "report_path": status["report_path"],
        "report_sha256": status["report_sha256"],
        "eligible_crawls": status["eligible_crawls"],
    }
    archived = archive_adoption_evidence(
        report_path,
        evidence,
        target_dir=target_dir,
    )
    tracker = tracker or ProgressTracker()
    stamped = tracker.stamp_unknown_completed(
        current_signature,
        evidence["eligible_crawls"],
        audit_id=evidence["audit_id"],
        audit_report_sha256=evidence["report_sha256"],
    )
    return {
        **status,
        "adopted": True,
        "rows_stamped": stamped,
        "archived_report": str(archived),
    }


def run_audit(
    plan: dict,
    settings,
    audit_dir: str | Path = AUDIT_DIR,
    sample_rate: float = AUDIT_SAMPLE_RATE,
    context=None,
    shutdown_event=None,
) -> dict:
    """Run selected sources against isolated state and publish only evaluation samples."""
    if not 0 <= sample_rate <= 1:
        raise ValueError("sample_rate must be between 0 and 1")
    if not plan.get("sources"):
        raise ValueError("audit plan contains no sources")
    if plan.get("filter_signature") != settings.filter_signature:
        raise ValueError("audit plan and runtime filter signatures do not match")

    if sample_rate <= 0:
        settings = replace(
            settings,
            shadow_samples_per_source=0,
            shadow_source_rate=0.0,
        )
    elif settings.shadow_samples_per_source > 0:
        settings = replace(settings, shadow_source_rate=1.0)

    root = Path(audit_dir) / settings.run_id
    if root.exists():
        raise FileExistsError(f"audit directory already exists: {root}")
    root.mkdir(parents=True)
    _atomic_json(root / "plan.json", plan)

    tracker = ProgressTracker(root / "progress.db")
    sources_by_crawl: dict[str, list[str]] = defaultdict(list)
    for source in plan["sources"]:
        sources_by_crawl[str(source["crawl_id"])].append(str(source["file_path"]))
    for crawl_id, source_files in sources_by_crawl.items():
        tracker.initialize_paths(source_files, crawl_id)

    crawl_ids = sorted(sources_by_crawl)
    provenance = build_run_manifest(
        settings,
        crawl_ids,
        "audit-stratified",
        len(plan["sources"]),
        int(plan["selection"]["per_crawl"]),
    )
    provenance["mode"] = "isolated_audit"
    provenance["historical_state_changed"] = False
    metrics = MetricsRecorder(
        profile=settings.profile_name,
        workers=settings.workers,
        inference_batch_size=settings.inference_batch_size,
        metrics_dir=METRICS_DIR,
        gpu_name=_gpu_name(),
        provenance=provenance,
    )
    metrics.add_target_files(len(plan["sources"]))
    writer = OutputWriter(
        root / "output",
        run_id=settings.run_id,
        filter_signature=settings.filter_signature,
    )
    sampler = DecisionSampler(
        EVALUATION_DIR / "candidate_samples.jsonl",
        sample_rate=sample_rate,
        representative=False,
    )
    context = context or multiprocessing.get_context("spawn")
    shutdown_event = shutdown_event or context.Event()
    service = None
    pipeline_entered = False
    try:
        service = InferenceService(
            settings,
            metrics,
            writer=writer,
            sampler=sampler,
        )
        with ExtractionPipeline(
            settings,
            context,
            metrics,
            shutdown_event=shutdown_event,
            service=service,
        ) as pipeline:
            pipeline_entered = True
            for crawl_id in crawl_ids:
                if shutdown_event.is_set():
                    break
                pipeline.process_crawl(
                    tracker,
                    get_crawl_info(crawl_id),
                    len(sources_by_crawl[crawl_id]),
                )
    finally:
        if service is not None and not pipeline_entered:
            service.close()
        metrics_snapshot = metrics.close()

    states = tracker.get_file_states(
        str(source["file_path"]) for source in plan["sources"]
    )
    comparison = compare_audit_outputs(
        plan,
        states,
        historical_output=OUTPUT_DIR,
        audit_output=root / "output",
    )
    report = {
        "schema_version": 1,
        "audit_id": settings.run_id,
        "created_at": _utc_now(),
        "audit_root": str(root),
        "filter_signature": settings.filter_signature,
        "sample_rate": sample_rate,
        "progress": tracker.get_summary(),
        "failures": tracker.get_failure_summary(examples_per_category=2),
        "metrics": metrics_snapshot,
        **comparison,
    }
    _atomic_json(root / "report.json", report)
    return report
