"""Command-line entry point for the Hometown XR Common Crawl extractor."""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import shutil
import signal
import sys
import threading
from dataclasses import replace

from config import (
    AUDIT_DEFAULT_PER_CRAWL,
    AUDIT_MAX_PER_CRAWL,
    AUDIT_SAMPLE_RATE,
    CACHE_DIR,
    DATA_DIR,
    DB_ARCHIVE_PATH,
    DB_PATH,
    DEFAULT_CRAWL_ID,
    DOMAIN_STORY_CAP,
    HARDWARE_PROFILES,
    LANG_DETECTION_THRESHOLD,
    LEASE_TIMEOUT_SECONDS,
    MODEL_BASELINE_PATH,
    OUTPUT_DIR,
    PARQUET_DIR,
    SEMANTIC_THRESHOLD,
    STORY_ENRICHMENT_MAX_WORKERS,
    STORY_ENRICHMENT_WORKERS,
    HardwareProfile,
    get_hardware_profile,
)
from crawl_catalog import (
    LEGACY_CRAWLS,
    get_all_crawl_ids,
    get_crawl_info,
    get_modern_crawls,
)
from downloader import fetch_file_paths
from metrics import MetricsRecorder, compare_profiles, print_latest, summarize_run_history
from output import OutputWriter
from pipeline import ExtractionPipeline
from progress import ProgressTracker
from run_lock import CrawlerRunLock
from runtime import RuntimeSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)-20s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

_shutdown_event = None
_shutdown_signal_count = 0


def _signal_handler(signum, frame) -> None:
    global _shutdown_signal_count
    del signum, frame
    if _shutdown_event:
        if _shutdown_signal_count:
            logger.warning("Second shutdown request received; exiting immediately.")
            raise SystemExit(1)
        _shutdown_signal_count = 1
        _shutdown_event.set()
    logger.info("Shutdown requested. Returning active sources to the checkpoint safely...")


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def _gpu_name() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except (ImportError, RuntimeError):
        pass
    return "CPU"


def process_crawl(
    crawl_id: str,
    limit: int | None,
    settings: RuntimeSettings,
    pipeline: ExtractionPipeline,
) -> tuple[int, int, int]:
    crawl_info = get_crawl_info(crawl_id)
    format_name = "ARC (HTML)" if crawl_info.era == "legacy" else "WET (text)"
    logger.info("--- Crawl: %s [%s] ---", crawl_id, format_name)

    tracker = ProgressTracker()
    tracker.recover_stale_leases(LEASE_TIMEOUT_SECONDS)
    summary = tracker.get_summary(crawl_id)
    if int(summary["total_files"]) == 0:
        logger.info("Fetching file list...")
        file_paths = fetch_file_paths(crawl_info)
        if not file_paths:
            logger.warning("No files found for %s. Skipping.", crawl_id)
            return 0, 0, 0
        tracker.initialize_paths(file_paths, crawl_id)
        summary = tracker.get_summary(crawl_id)
    logger.info(
        "Progress: %s/%s completed, %s pending, %s retryable, %s matches",
        summary["completed"],
        summary["total_files"],
        summary["pending"],
        summary["retryable"],
        summary["total_matches"],
    )
    ready = int(summary["ready"])
    target = min(limit, ready) if limit is not None else ready
    if target <= 0:
        logger.info("No ready files to process for %s.", crawl_id)
        return 0, 0, 0

    pipeline.metrics.add_target_files(target)
    logger.info(
        "Running %s CPU parsers into one GPU inference service (%s sources)...",
        settings.workers,
        target,
    )
    completed, matches = pipeline.process_crawl(tracker, crawl_info, target)
    return completed, matches, target


def _schedule_order(
    crawl_ids: list[str],
    strategy: str,
    summaries: list[dict] | None = None,
) -> list[str]:
    if strategy == "oldest":
        return list(crawl_ids)
    if strategy == "newest":
        return list(reversed(crawl_ids))
    if strategy == "yield-aware":
        from scheduling import yield_aware_order

        return yield_aware_order(crawl_ids, summaries or [])
    if strategy != "round-robin":
        raise ValueError(f"unknown scheduling strategy: {strategy}")
    ordered = []
    left = 0
    right = len(crawl_ids) - 1
    take_newest = True
    while left <= right:
        if take_newest:
            ordered.append(crawl_ids[right])
            right -= 1
        else:
            ordered.append(crawl_ids[left])
            left += 1
        take_newest = not take_newest
    return ordered


def run(
    crawl_ids: list[str],
    limit: int | None,
    settings: RuntimeSettings,
    strategy: str = "round-robin",
    chunk_size: int = 100,
) -> None:
    global _shutdown_event, _shutdown_signal_count
    context = multiprocessing.get_context("spawn")
    _shutdown_event = context.Event()
    _shutdown_signal_count = 0
    from signatures import build_run_manifest

    effective_strategy = strategy if len(crawl_ids) > 1 else "oldest"
    run_manifest = build_run_manifest(
        settings, crawl_ids, effective_strategy, limit, chunk_size
    )
    metrics = MetricsRecorder(
        profile=settings.profile_name,
        workers=settings.workers,
        inference_batch_size=settings.inference_batch_size,
        gpu_name=_gpu_name(),
        provenance=run_manifest,
    )

    try:
        with CrawlerRunLock(settings.profile_name):
            OutputWriter().cleanup_stale_staging()
            logger.info("=" * 70)
            logger.info("Hometown XR Common Crawl Extractor")
            logger.info("Crawls: %s", len(crawl_ids))
            logger.info("Profile: %s", settings.profile_name)
            logger.info("CPU parser workers: %s", settings.workers)
            logger.info("Candidate batch: %s", settings.candidate_batch_size)
            logger.info("Shared inference batch: %s", settings.inference_batch_size)
            logger.info("Model precision: %s", settings.precision)
            logger.info("Adaptive batching: %s", settings.adaptive_batching)
            logger.info("Inference cache: %s", settings.cache_enabled)
            logger.info("Semantic threshold: %s", settings.semantic_threshold)
            logger.info("Run ID: %s", settings.run_id)
            logger.info("Filter signature: %s", settings.filter_signature[:16])
            logger.info("Scheduling: %s (chunk size %s)", effective_strategy, chunk_size)
            logger.info("=" * 70)

            total_files = 0
            total_matches = 0
            crawls_attempted = 0
            sources_scheduled = 0
            with ExtractionPipeline(
                settings,
                context,
                metrics,
                shutdown_event=_shutdown_event,
            ) as pipeline:
                tracker = ProgressTracker()
                active = _schedule_order(
                    crawl_ids,
                    effective_strategy,
                    tracker.get_per_crawl_summary(),
                )
                chunked = effective_strategy in {"round-robin", "yield-aware"}
                while active and not _shutdown_event.is_set():
                    next_round = []
                    for crawl_id in active:
                        if _shutdown_event.is_set():
                            break
                        remaining = None if limit is None else limit - sources_scheduled
                        if remaining is not None and remaining <= 0:
                            break
                        per_crawl_limit = remaining
                        if chunked:
                            per_crawl_limit = min(chunk_size, remaining or chunk_size)
                        crawls_attempted += 1
                        files, matches, scheduled = process_crawl(
                            crawl_id,
                            per_crawl_limit,
                            settings,
                            pipeline,
                        )
                        total_files += files
                        total_matches += matches
                        sources_scheduled += scheduled
                        if chunked and scheduled > 0:
                            next_round.append(crawl_id)
                    if not chunked or (
                        limit is not None and sources_scheduled >= limit
                    ):
                        break
                    active = _schedule_order(
                        next_round,
                        effective_strategy,
                        tracker.get_per_crawl_summary(),
                    )

            logger.info("=" * 70)
            logger.info("Crawl chunks attempted: %s", crawls_attempted)
            logger.info("Sources scheduled: %s", sources_scheduled)
            logger.info("Files completed: %s", total_files)
            logger.info("Matches committed: %s", total_matches)
            logger.info("=" * 70)
    finally:
        metrics.close()
        _shutdown_event = None
        _shutdown_signal_count = 0


def show_status() -> None:
    summary = ProgressTracker().get_summary()
    print("\n" + "=" * 62)
    print("  Hometown XR Extractor - Overall Status")
    print("=" * 62)
    print(f"  Total files:       {summary['total_files']}")
    print(f"  Completed:         {summary['completed']}")
    print(f"  Pending:           {summary['pending']}")
    print(f"  Processing:        {summary['processing']}")
    print(f"  Failed:            {summary['failed']}")
    print(f"  Retryable now:     {summary['retryable']}")
    print(f"  Attempts exhausted:{summary['exhausted']:>8}")
    print(f"  Progress:          {summary['progress_pct']:.2f}%")
    print("  ----------------------")
    print(f"  Records processed: {summary['total_records']:,}")
    print(f"  Matches found:     {summary['total_matches']:,}")
    print("=" * 62)

    rows = ProgressTracker().get_per_crawl_summary()
    if rows:
        print("\n  Per-Crawl Breakdown:")
        print(f"  {'Crawl ID':<25} {'Done':>8} {'Total':>8} {'Failed':>8} {'Matches':>10}")
        for row in rows:
            print(
                f"  {row['crawl_id']:<25} {row['completed']:>8} "
                f"{row['total']:>8} {row['failed']:>8} {row['matches']:>10}"
            )
    print()


def list_crawls() -> None:
    print("\n" + "=" * 60)
    print("  Available Common Crawl Datasets")
    print("=" * 60)
    print("\n  LEGACY CRAWLS (2008-2012) - ARC format")
    for crawl in LEGACY_CRAWLS:
        print(f"  {crawl.crawl_id:<25} {crawl.notes}")

    print("\n  MODERN CRAWLS (2013-present) - WET format")
    current_year = None
    modern_crawls = get_modern_crawls()
    for crawl_id in reversed(modern_crawls):
        year = crawl_id.split("-")[2]
        if year != current_year:
            current_year = year
            print(f"\n  {year}:")
        print(f"    {crawl_id}")
    print(f"\n  Total: {len(LEGACY_CRAWLS) + len(modern_crawls)} crawls\n")


def retry_failed(
    crawl_id: str | None,
    limit: int | None = None,
    category: str | None = None,
) -> None:
    with CrawlerRunLock("maintenance"):
        count = ProgressTracker().retry_failed(crawl_id, limit=limit, category=category)
    scope = crawl_id or "all crawls"
    qualifier = f" in category {category}" if category else ""
    print(f"Reset {count} failed files for immediate retry in {scope}{qualifier}.")


def show_failures(crawl_id: str | None, examples: int) -> None:
    result = ProgressTracker().get_failure_summary(crawl_id, examples)
    print(json.dumps(result, indent=2))


def recover_leases(minutes: int) -> None:
    with CrawlerRunLock("maintenance"):
        count = ProgressTracker().recover_stale_leases(minutes * 60)
    print(f"Recovered {count} stale processing leases.")


def reset_data() -> None:
    with CrawlerRunLock("maintenance"):
        if DB_PATH.exists():
            DB_PATH.unlink()
        DB_ARCHIVE_PATH.unlink(missing_ok=True)
        for directory in (OUTPUT_DIR, DATA_DIR / "exports", PARQUET_DIR, CACHE_DIR):
            if directory.exists():
                shutil.rmtree(directory)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("All extracted output, derivatives, and progress have been reset.")


def doctor(profile_name: str) -> int:
    profile = get_hardware_profile(profile_name)
    errors = []
    print("Hometown XR environment check")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  Profile: {profile.name}")
    print(f"  Workers: {profile.workers}")
    print(f"  Candidate batch: {profile.candidate_batch_size}")
    print(f"  Inference batch: {profile.inference_batch_size}")
    print(f"  Encoding batch: {profile.encoding_batch_size}")
    print(f"  Precision: {profile.precision}")
    print(f"  Database: {'present' if DB_PATH.exists() else 'not created'}")
    print(f"  Output directory: {OUTPUT_DIR}")
    try:
        import torch

        print(f"  PyTorch: {torch.__version__}")
        cuda_available = torch.cuda.is_available()
        print(f"  CUDA available: {cuda_available}")
        cuda_runtime = getattr(torch.version, "cuda", None)
        print(f"  PyTorch CUDA runtime: {cuda_runtime or 'none'}")
        if cuda_available:
            gpu_name = torch.cuda.get_device_name(0)
            capability = torch.cuda.get_device_capability(0)
            print(f"  GPU: {gpu_name}")
            print(f"  CUDA capability: {capability[0]}.{capability[1]}")
        else:
            gpu_name = ""
            capability = (0, 0)

        if profile.name in {"3080", "4090"}:
            if not cuda_available:
                errors.append(f"The {profile.name} profile requires CUDA-enabled PyTorch.")
            elif profile.name not in gpu_name:
                errors.append(
                    f"The {profile.name} profile selected a different GPU: {gpu_name}."
                )
            if cuda_runtime != "12.1":
                errors.append(
                    f"The {profile.name} profile requires the tracked CUDA 12.1 runtime; "
                    f"found {cuda_runtime or 'none'}."
                )
        else:
            if not cuda_available:
                errors.append("The 5090 profile requires a CUDA-enabled PyTorch build.")
            else:
                if "5090" not in gpu_name:
                    errors.append(
                        f"The 5090 profile selected a different GPU: {gpu_name}."
                    )
                if capability < (12, 0):
                    errors.append(
                        "The detected GPU does not expose Blackwell compute capability 12.0."
                    )
            try:
                cuda_version = tuple(int(part) for part in str(cuda_runtime).split(".")[:2])
            except (TypeError, ValueError):
                cuda_version = ()
            if cuda_version < (12, 8):
                errors.append(
                    "RTX 5090 requires a PyTorch CUDA 12.8+ build; "
                    f"found {cuda_runtime or 'none'}."
                )
    except ImportError:
        print("  PyTorch: missing")
        return 1
    for error in errors:
        print(f"  ERROR: {error}")
    return 1 if errors else 0


def verify_output() -> int:
    from checkpoint import verify_output_integrity

    with CrawlerRunLock("verify-output"):
        result = verify_output_integrity()
        for failure in result["source_failures"][:20]:
            print(f"{failure['source_file']}: {'; '.join(failure['errors'])}")
        for relative in result["uncovered_shards"][:20]:
            print(f"Missing manifest coverage: {relative}")
        omitted = max(0, len(result["source_failures"]) - 20) + max(
            0, len(result["uncovered_shards"]) - 20
        )
        if omitted > 0:
            print(f"... and {omitted} more errors")
        print(
            f"Verified {result['manifests']} source manifests and {result['shards']} shards; "
            f"{result['integrity_errors']} integrity errors."
        )
        return 0 if result["valid"] else 1


def _runtime_settings(args) -> RuntimeSettings:
    profile: HardwareProfile = get_hardware_profile(args.profile)
    settings = RuntimeSettings(
        profile_name=profile.name,
        workers=args.workers or profile.workers,
        candidate_batch_size=args.candidate_batch_size or profile.candidate_batch_size,
        inference_batch_size=args.inference_batch_size or profile.inference_batch_size,
        encoding_batch_size=args.encoding_batch_size or profile.encoding_batch_size,
        semantic_threshold=args.threshold,
        language_threshold=args.language_threshold,
        precision=profile.precision if args.precision == "auto" else args.precision,
        adaptive_batching=not args.no_adaptive_batching,
        cache_enabled=not args.no_cache,
        filter_signature="",
        run_id="",
    )
    if settings.workers <= 0:
        raise ValueError("workers must be positive")
    if min(
        settings.candidate_batch_size,
        settings.inference_batch_size,
        settings.encoding_batch_size,
    ) <= 0:
        raise ValueError("batch sizes must be positive")
    if not 0 <= settings.semantic_threshold <= 1:
        raise ValueError("semantic threshold must be between 0 and 1")
    if not 0 <= settings.language_threshold <= 1:
        raise ValueError("language threshold must be between 0 and 1")
    from signatures import build_filter_signature, new_run_id

    return replace(
        settings,
        filter_signature=build_filter_signature(
            settings.semantic_threshold,
            settings.language_threshold,
        ),
        run_id=new_run_id(),
    )


def _filter_command(args) -> None:
    from signatures import build_filter_signature, filter_contract

    signature = build_filter_signature(args.threshold, args.language_threshold)
    tracker = ProgressTracker()
    if args.filter_command == "status":
        result = tracker.get_filter_signature_summary(signature)
        result["recent_adoptions"] = tracker.get_signature_adoptions()
        contract = filter_contract(args.threshold, args.language_threshold)
        result["contract"] = {
            "schema_version": contract["schema_version"],
            "semantic_model": contract["semantic_model"],
            "semantic_threshold": contract["semantic_threshold"],
            "language_threshold": contract["language_threshold"],
            "paragraph_length": contract["paragraph_length"],
            "narrative_filter": contract["narrative_filter"],
            "keyword_count": len(contract["keywords"]),
            "concept_anchor_count": len(contract["concept_anchors"]),
        }
    elif not args.yes:
        raise SystemExit("Refusing to change checkpoint state without --yes")
    elif args.filter_command == "stamp-current":
        from audit import archive_adoption_evidence, load_adoption_evidence

        evidence = load_adoption_evidence(
            args.audit_report,
            signature,
            requested_crawls=args.crawl,
        )
        evidence["archived_report"] = str(
            archive_adoption_evidence(args.audit_report, evidence)
        )
        result = {
            "current_signature": signature,
            "stamped": tracker.stamp_unknown_completed(
                signature,
                evidence["eligible_crawls"],
                audit_id=evidence["audit_id"],
                audit_report_sha256=evidence["report_sha256"],
            ),
            "evidence": evidence,
        }
    else:
        result = {
            "current_signature": signature,
            "reset": tracker.reset_stale_completed(
                signature,
                include_unknown=args.include_unknown,
                crawl_id=args.crawl,
                limit=args.limit,
            ),
        }
    print(json.dumps(result, indent=2))


def _evaluation_command(args) -> None:
    from evaluation import (
        annotate,
        build_annotation_sample,
        compact_replay_reservoir,
        evaluation_plan,
        evaluation_report,
        evaluation_status,
        multilingual_recall_report,
        undo_annotation,
    )

    if args.evaluation_command == "sample":
        print(json.dumps(build_annotation_sample(size=args.size), indent=2))
    elif args.evaluation_command == "annotate":
        print(
            json.dumps(
                annotate(
                    language=args.language,
                    limit=args.limit,
                    predicted_accept=(
                        None if args.prediction is None else args.prediction == "accepted"
                    ),
                    split=args.split,
                    sample_id=args.sample_id,
                    relabel=args.relabel,
                    annotator=args.annotator,
                    quick=args.quick,
                ),
                indent=2,
            )
        )
    elif args.evaluation_command == "report":
        print(json.dumps(evaluation_report(), indent=2))
    elif args.evaluation_command == "status":
        print(json.dumps(evaluation_status(), indent=2))
    elif args.evaluation_command == "plan":
        print(json.dumps(evaluation_plan(), indent=2))
    elif args.evaluation_command == "replay":
        print(json.dumps(compact_replay_reservoir(), indent=2))
    elif args.evaluation_command == "undo":
        print(json.dumps(undo_annotation(sample_id=args.sample_id), indent=2))
    elif args.evaluation_command == "multilingual":
        print(json.dumps(multilingual_recall_report(), ensure_ascii=False, indent=2))
    elif args.evaluation_command == "serve":
        from annotation_workbench import serve_annotation_workbench

        serve_annotation_workbench(
            host=args.host,
            port=args.port,
            open_browser=args.open_browser,
        )


def _audit_command(args) -> None:
    global _shutdown_event, _shutdown_signal_count
    from audit import build_audit_plan, run_audit
    from signatures import build_filter_signature

    if args.audit_command == "run" and not args.yes:
        raise SystemExit("Refusing to download and run an audit without --yes")
    if args.audit_command == "run":
        settings = _runtime_settings(args)
        signature = settings.filter_signature
    else:
        settings = None
        signature = build_filter_signature(args.threshold, args.language_threshold)
    plan = build_audit_plan(
        signature,
        per_crawl=args.per_crawl,
        crawl_ids=args.crawl,
        include_current=args.include_current,
    )
    if args.audit_command == "plan":
        print(json.dumps(plan, indent=2))
        return
    context = multiprocessing.get_context("spawn")
    _shutdown_event = context.Event()
    _shutdown_signal_count = 0
    try:
        with CrawlerRunLock("audit"):
            print(
                json.dumps(
                    run_audit(
                        plan,
                        settings,
                        sample_rate=args.sample_rate,
                        context=context,
                        shutdown_event=_shutdown_event,
                    ),
                    indent=2,
                )
            )
    finally:
        _shutdown_event = None
        _shutdown_signal_count = 0


def main() -> None:
    global _shutdown_event, _shutdown_signal_count
    parser = argparse.ArgumentParser(
        description="Extract personal home and belonging narratives from Common Crawl"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="start or resume processing")
    run_parser.add_argument("--crawl")
    run_parser.add_argument("--all", action="store_true")
    run_parser.add_argument("--limit", type=int)
    run_parser.add_argument(
        "--strategy",
        choices=["round-robin", "yield-aware", "newest", "oldest"],
        default="round-robin",
    )
    run_parser.add_argument("--chunk-size", type=int, default=100)
    run_parser.add_argument("--threshold", type=float, default=SEMANTIC_THRESHOLD)
    run_parser.add_argument("--language-threshold", type=float, default=LANG_DETECTION_THRESHOLD)
    run_parser.add_argument("--profile", choices=["auto", *HARDWARE_PROFILES], default="auto")
    run_parser.add_argument("--workers", type=int)
    run_parser.add_argument(
        "--candidate-batch-size",
        "--stream-batch-size",
        dest="candidate_batch_size",
        type=int,
    )
    run_parser.add_argument("--inference-batch-size", type=int)
    run_parser.add_argument("--encoding-batch-size", type=int)
    run_parser.add_argument("--precision", choices=["auto", "fp32", "fp16"], default="auto")
    run_parser.add_argument("--no-adaptive-batching", action="store_true")
    run_parser.add_argument("--no-cache", action="store_true")

    subparsers.add_parser("status", help="show processing progress")
    health_parser = subparsers.add_parser(
        "health", help="show consolidated workstation and project readiness"
    )
    health_parser.add_argument(
        "--profile", choices=["auto", *HARDWARE_PROFILES], default="auto"
    )
    health_parser.add_argument("--full", action="store_true")
    health_parser.add_argument("--strict", action="store_true")
    metrics_parser = subparsers.add_parser(
        "metrics", help="show current or historical operational metrics"
    )
    metrics_mode = metrics_parser.add_mutually_exclusive_group()
    metrics_mode.add_argument("--history", action="store_true")
    metrics_mode.add_argument("--compare-profiles", action="store_true")
    metrics_parser.add_argument("--profile", choices=sorted(HARDWARE_PROFILES))
    metrics_parser.add_argument("--limit", type=int, default=20)
    metrics_parser.add_argument("--full", action="store_true")
    subparsers.add_parser("list", help="list available crawls")
    subparsers.add_parser("reset", help="wipe output and progress")
    subparsers.add_parser("verify-output", help="verify source shard checksums")

    database_parser = subparsers.add_parser(
        "database", help="manage the compressed cross-PC progress checkpoint"
    )
    database_subparsers = database_parser.add_subparsers(
        dest="database_command", required=True
    )
    database_subparsers.add_parser("archive")
    database_subparsers.add_parser("restore")
    database_subparsers.add_parser("check")

    checkpoint_parser = subparsers.add_parser(
        "checkpoint",
        help="verify and compact durable state for handoff",
    )
    checkpoint_parser.add_argument("--no-verify", action="store_true")
    checkpoint_parser.add_argument("--no-compact-manifests", action="store_true")
    checkpoint_parser.add_argument("--no-compact-db", action="store_true")
    checkpoint_parser.add_argument("--force-vacuum", action="store_true")

    cache_parser = subparsers.add_parser("cache", help="inspect or clear local inference cache")
    cache_subparsers = cache_parser.add_subparsers(dest="cache_command", required=True)
    cache_subparsers.add_parser("stats")
    cache_subparsers.add_parser("clear")

    retry_parser = subparsers.add_parser("retry", help="retry failed files now")
    retry_scope = retry_parser.add_mutually_exclusive_group()
    retry_scope.add_argument("--crawl")
    retry_scope.add_argument("--all", action="store_true")
    retry_parser.add_argument("--limit", type=int)
    retry_parser.add_argument(
        "--category",
        choices=[
            "connection",
            "http_404",
            "http_429",
            "http_500",
            "http_502",
            "http_503",
            "http_504",
            "inference",
            "other",
            "output",
            "process_pool",
            "timeout",
        ],
    )

    failures_parser = subparsers.add_parser(
        "failures", help="summarize failed sources by operational category"
    )
    failures_parser.add_argument("--crawl")
    failures_parser.add_argument("--examples", type=int, default=3)

    recover_parser = subparsers.add_parser(
        "recover", help="release processing leases older than a threshold"
    )
    recover_parser.add_argument("--minutes", type=int, default=LEASE_TIMEOUT_SECONDS // 60)

    doctor_parser = subparsers.add_parser("doctor", help="check the local runtime")
    doctor_parser.add_argument("--profile", choices=["auto", *HARDWARE_PROFILES], default="auto")

    benchmark_parser = subparsers.add_parser("benchmark", help="benchmark and tune this PC")
    benchmark_parser.add_argument("--profile", choices=["auto", *HARDWARE_PROFILES], default="auto")
    benchmark_parser.add_argument("--quick", action="store_true")
    benchmark_parser.add_argument("--no-write", action="store_true")
    benchmark_parser.add_argument("--real", action="store_true")
    benchmark_parser.add_argument("--crawl", default=DEFAULT_CRAWL_ID)
    benchmark_parser.add_argument("--sources", type=int, default=5)
    benchmark_parser.add_argument("--worker-count", type=int, action="append")
    benchmark_parser.add_argument("--apply", action="store_true")

    model_parser = subparsers.add_parser(
        "model-validation",
        help="capture or compare semantic-model regression snapshots",
    )
    model_subparsers = model_parser.add_subparsers(
        dest="model_validation_command",
        required=True,
    )
    model_capture = model_subparsers.add_parser("capture")
    model_capture.add_argument("--annotations")
    model_capture.add_argument("--output", default=str(MODEL_BASELINE_PATH))
    model_capture.add_argument(
        "--profile", choices=["auto", *HARDWARE_PROFILES], default="auto"
    )
    model_capture.add_argument("--limit", type=int)
    model_compare = model_subparsers.add_parser("compare")
    model_compare.add_argument("--baseline", default=str(MODEL_BASELINE_PATH))
    model_compare.add_argument("--candidate", required=True)
    model_compare.add_argument("--output")
    model_compare.add_argument("--max-score-drift", type=float, default=0.005)
    model_compare.add_argument("--minimum-concept-agreement", type=float, default=0.99)
    model_compare.add_argument("--minimum-threshold-agreement", type=float, default=1.0)

    parquet_parser = subparsers.add_parser("parquet", help="export partitioned Parquet")
    parquet_parser.add_argument("--dedupe", choices=["none", "exact", "near"], default="exact")
    parquet_parser.add_argument("--near-distance", type=int, default=3)
    parquet_parser.add_argument("--domain-warning-share", type=float, default=0.10)
    parquet_parser.add_argument("--domain-story-cap", type=int, default=DOMAIN_STORY_CAP)

    evaluation_parser = subparsers.add_parser("evaluation", help="sample and evaluate filters")
    evaluation_subparsers = evaluation_parser.add_subparsers(
        dest="evaluation_command", required=True
    )
    sample_parser = evaluation_subparsers.add_parser("sample")
    sample_parser.add_argument("--size", type=int, default=400)
    annotate_parser = evaluation_subparsers.add_parser("annotate")
    annotate_parser.add_argument("--language")
    annotate_parser.add_argument("--limit", type=int)
    annotate_parser.add_argument("--prediction", choices=["accepted", "rejected"])
    annotate_parser.add_argument("--split", choices=["all", "tuning", "holdout"])
    annotate_parser.add_argument("--sample-id")
    annotate_parser.add_argument("--relabel", action="store_true")
    annotate_parser.add_argument("--annotator")
    annotate_parser.add_argument("--quick", action="store_true")
    evaluation_subparsers.add_parser("report")
    evaluation_subparsers.add_parser("status")
    evaluation_subparsers.add_parser("plan")
    evaluation_subparsers.add_parser("replay")
    evaluation_subparsers.add_parser("multilingual")
    serve_parser = evaluation_subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--open-browser", action="store_true")
    undo_parser = evaluation_subparsers.add_parser("undo")
    undo_parser.add_argument("--sample-id")

    stories_parser = subparsers.add_parser(
        "stories",
        help="expand precise paragraph matches into bounded source stories",
    )
    stories_subparsers = stories_parser.add_subparsers(
        dest="stories_command",
        required=True,
    )
    stories_subparsers.add_parser(
        "stop",
        help="request a graceful stop from another terminal",
    )
    for action in ("plan", "enrich", "status"):
        story_action = stories_subparsers.add_parser(action)
        story_action.add_argument("--crawl", action="append")
        story_action.add_argument("--source", action="append")
        story_scope = story_action.add_mutually_exclusive_group()
        story_scope.add_argument("--limit", type=int, default=10)
        story_scope.add_argument("--all", action="store_true")
        if action == "enrich":
            story_action.add_argument("--yes", action="store_true")
            story_action.add_argument(
                "--workers",
                type=int,
                default=STORY_ENRICHMENT_WORKERS,
            )
    stories_export_parser = stories_subparsers.add_parser("export")
    stories_export_parser.add_argument("--include-short", action="store_true")

    audit_parser = subparsers.add_parser(
        "audit", help="plan or run an isolated audit of completed sources"
    )
    audit_subparsers = audit_parser.add_subparsers(dest="audit_command", required=True)
    audit_plan_parser = audit_subparsers.add_parser("plan")
    audit_plan_parser.add_argument("--per-crawl", type=int, default=AUDIT_DEFAULT_PER_CRAWL)
    audit_plan_parser.add_argument("--crawl", action="append")
    audit_plan_parser.add_argument("--include-current", action="store_true")
    audit_plan_parser.add_argument("--threshold", type=float, default=SEMANTIC_THRESHOLD)
    audit_plan_parser.add_argument(
        "--language-threshold", type=float, default=LANG_DETECTION_THRESHOLD
    )

    audit_run_parser = audit_subparsers.add_parser("run")
    audit_run_parser.add_argument("--per-crawl", type=int, default=AUDIT_DEFAULT_PER_CRAWL)
    audit_run_parser.add_argument("--crawl", action="append")
    audit_run_parser.add_argument("--include-current", action="store_true")
    audit_run_parser.add_argument("--threshold", type=float, default=SEMANTIC_THRESHOLD)
    audit_run_parser.add_argument(
        "--language-threshold", type=float, default=LANG_DETECTION_THRESHOLD
    )
    audit_run_parser.add_argument(
        "--profile", choices=["auto", *HARDWARE_PROFILES], default="auto"
    )
    audit_run_parser.add_argument("--workers", type=int)
    audit_run_parser.add_argument("--candidate-batch-size", type=int)
    audit_run_parser.add_argument("--inference-batch-size", type=int)
    audit_run_parser.add_argument("--encoding-batch-size", type=int)
    audit_run_parser.add_argument(
        "--precision", choices=["auto", "fp32", "fp16"], default="auto"
    )
    audit_run_parser.add_argument("--no-adaptive-batching", action="store_true")
    audit_run_parser.add_argument("--no-cache", action="store_true")
    audit_run_parser.add_argument("--sample-rate", type=float, default=AUDIT_SAMPLE_RATE)
    audit_run_parser.add_argument("--yes", action="store_true")

    filter_parser = subparsers.add_parser(
        "filters", help="inspect or selectively refresh filter-signature state"
    )
    filter_parser.add_argument("--threshold", type=float, default=SEMANTIC_THRESHOLD)
    filter_parser.add_argument(
        "--language-threshold", type=float, default=LANG_DETECTION_THRESHOLD
    )
    filter_subparsers = filter_parser.add_subparsers(dest="filter_command", required=True)
    filter_subparsers.add_parser("status")
    stamp_parser = filter_subparsers.add_parser("stamp-current")
    stamp_parser.add_argument("--audit-report", required=True)
    stamp_parser.add_argument("--crawl", action="append")
    stamp_parser.add_argument("--yes", action="store_true")
    reset_filter_parser = filter_subparsers.add_parser("reset-stale")
    reset_filter_parser.add_argument("--include-unknown", action="store_true")
    reset_filter_parser.add_argument("--crawl")
    reset_filter_parser.add_argument("--limit", type=int)
    reset_filter_parser.add_argument("--yes", action="store_true")

    args = parser.parse_args()
    if args.command == "run":
        if args.limit is not None and args.limit <= 0:
            parser.error("--limit must be positive")
        if args.chunk_size <= 0:
            parser.error("--chunk-size must be positive")
        settings = _runtime_settings(args)
        crawl_ids = get_all_crawl_ids() if args.all else [args.crawl or DEFAULT_CRAWL_ID]
        run(crawl_ids, args.limit, settings, args.strategy, args.chunk_size)
    elif args.command == "status":
        show_status()
    elif args.command == "health":
        from project_health import collect_project_health

        result = collect_project_health(args.profile, full=args.full)
        print(json.dumps(result, indent=2))
        if args.strict and result["status"] == "fail":
            raise SystemExit(1)
    elif args.command == "metrics":
        if args.limit <= 0:
            parser.error("--limit must be positive")
        if args.compare_profiles:
            print(json.dumps(compare_profiles(), indent=2))
        elif args.history:
            print(
                json.dumps(
                    summarize_run_history(args.limit, profile=args.profile),
                    indent=2,
                )
            )
        else:
            print_latest(full=args.full)
    elif args.command == "list":
        list_crawls()
    elif args.command == "retry":
        if args.limit is not None and args.limit <= 0:
            parser.error("--limit must be positive")
        retry_failed(
            None if args.all else (args.crawl or DEFAULT_CRAWL_ID),
            limit=args.limit,
            category=args.category,
        )
    elif args.command == "failures":
        if args.examples < 0:
            parser.error("--examples cannot be negative")
        show_failures(args.crawl, args.examples)
    elif args.command == "recover":
        if args.minutes < 0:
            parser.error("--minutes cannot be negative")
        recover_leases(args.minutes)
    elif args.command == "reset":
        reset_data()
    elif args.command == "doctor":
        raise SystemExit(doctor(args.profile))
    elif args.command == "verify-output":
        raise SystemExit(verify_output())
    elif args.command == "checkpoint":
        from checkpoint import create_checkpoint

        with CrawlerRunLock("checkpoint"):
            print(
                json.dumps(
                    create_checkpoint(
                        verify=not args.no_verify,
                        compact_manifests=not args.no_compact_manifests,
                        compact_database=not args.no_compact_db,
                        force_vacuum=args.force_vacuum,
                    ),
                    indent=2,
                )
            )
    elif args.command == "cache":
        from inference_cache import InferenceCache

        with CrawlerRunLock("cache-maintenance"):
            with InferenceCache() as cache:
                if args.cache_command == "clear":
                    cache.clear()
                print(json.dumps(cache.stats(), indent=2))
    elif args.command == "benchmark":
        from benchmark import run_benchmark, run_workload_benchmark

        with CrawlerRunLock("benchmark"):
            if args.sources <= 0 or args.sources > AUDIT_MAX_PER_CRAWL:
                parser.error(
                    f"--sources must be between 1 and {AUDIT_MAX_PER_CRAWL}"
                )
            if args.worker_count and min(args.worker_count) <= 0:
                parser.error("--worker-count must be positive")
            if args.apply and not args.real:
                parser.error("--apply is only valid with --real")
            result = (
                run_workload_benchmark(
                    args.profile,
                    args.crawl,
                    source_count=args.sources,
                    worker_counts=args.worker_count,
                    write=args.apply,
                )
                if args.real
                else run_benchmark(
                    args.profile,
                    quick=args.quick,
                    write=not args.no_write,
                )
            )
            print(
                json.dumps(result, indent=2)
            )
    elif args.command == "model-validation":
        from model_regression import capture_model_snapshot, compare_model_snapshots

        if args.model_validation_command == "capture":
            if args.limit is not None and args.limit <= 0:
                parser.error("--limit must be positive")
            keyword_arguments = {
                "output_path": args.output,
                "profile_name": args.profile,
                "limit": args.limit,
            }
            if args.annotations:
                keyword_arguments["annotation_path"] = args.annotations
            with CrawlerRunLock("model-validation"):
                result = capture_model_snapshot(**keyword_arguments)
            result = {key: value for key, value in result.items() if key != "samples"}
        else:
            if args.max_score_drift < 0:
                parser.error("--max-score-drift cannot be negative")
            for name in (
                "minimum_concept_agreement",
                "minimum_threshold_agreement",
            ):
                if not 0 <= getattr(args, name) <= 1:
                    parser.error(f"--{name.replace('_', '-')} must be between 0 and 1")
            result = compare_model_snapshots(
                args.baseline,
                args.candidate,
                output_path=args.output,
                max_score_drift=args.max_score_drift,
                minimum_concept_agreement=args.minimum_concept_agreement,
                minimum_threshold_agreement=args.minimum_threshold_agreement,
            )
        print(json.dumps(result, indent=2))
        if args.model_validation_command == "compare" and not result["safe"]:
            raise SystemExit(1)
    elif args.command == "parquet":
        from parquet_export import export_parquet

        if not 0 < args.domain_warning_share <= 1:
            parser.error("--domain-warning-share must be between 0 and 1")
        if args.domain_story_cap <= 0:
            parser.error("--domain-story-cap must be positive")
        with CrawlerRunLock("parquet"):
            print(
                json.dumps(
                    export_parquet(
                        dedupe=args.dedupe,
                        near_distance=args.near_distance,
                        domain_share_warning=args.domain_warning_share,
                        domain_story_cap=args.domain_story_cap,
                    ),
                    indent=2,
                )
            )
    elif args.command == "evaluation":
        if getattr(args, "limit", None) is not None and args.limit <= 0:
            parser.error("--limit must be positive")
        port = getattr(args, "port", None)
        if port is not None and not 1 <= port <= 65535:
            parser.error("--port must be between 1 and 65535")
        _evaluation_command(args)
    elif args.command == "stories":
        from story_control import request_story_shutdown, watch_story_shutdown
        from story_enrichment import (
            enrich_story_sources,
            export_stories,
            plan_story_enrichment,
        )

        if args.stories_command in {"plan", "enrich", "status"}:
            limit = None if args.all else args.limit
            if limit is not None and limit <= 0:
                parser.error("--limit must be positive")
        else:
            limit = None
        if args.stories_command == "stop":
            result = request_story_shutdown()
        elif args.stories_command == "enrich":
            if not args.yes:
                parser.error("story enrichment downloads source files; pass --yes")
            if not 1 <= args.workers <= STORY_ENRICHMENT_MAX_WORKERS:
                parser.error(
                    f"--workers must be between 1 and {STORY_ENRICHMENT_MAX_WORKERS}"
                )
            _shutdown_event = threading.Event()
            _shutdown_signal_count = 0
            try:
                with CrawlerRunLock("story-enrichment"):
                    with watch_story_shutdown(_shutdown_event):
                        result = enrich_story_sources(
                            crawl_ids=args.crawl,
                            source_files=args.source,
                            limit=limit,
                            workers=args.workers,
                            shutdown_event=_shutdown_event,
                        )
            finally:
                _shutdown_event = None
                _shutdown_signal_count = 0
        elif args.stories_command == "export":
            result = export_stories(include_short=args.include_short)
        else:
            result = plan_story_enrichment(
                crawl_ids=getattr(args, "crawl", None),
                source_files=getattr(args, "source", None),
                limit=limit,
            )
            if args.stories_command == "status":
                result["shown_sources"] = min(len(result["selection"]), 10)
                if len(result["selection"]) > 10:
                    result["selection"] = result["selection"][:10]
                    result["selection_truncated"] = True
        print(json.dumps(result, indent=2))
    elif args.command == "audit":
        if not 1 <= args.per_crawl <= AUDIT_MAX_PER_CRAWL:
            parser.error(f"--per-crawl must be between 1 and {AUDIT_MAX_PER_CRAWL}")
        if not 0 <= args.threshold <= 1:
            parser.error("--threshold must be between 0 and 1")
        if not 0 <= args.language_threshold <= 1:
            parser.error("--language-threshold must be between 0 and 1")
        if args.audit_command == "run" and not 0 <= args.sample_rate <= 1:
            parser.error("--sample-rate must be between 0 and 1")
        _audit_command(args)
    elif args.command == "filters":
        if getattr(args, "limit", None) is not None and args.limit <= 0:
            parser.error("--limit must be positive")
        if not 0 <= args.threshold <= 1:
            parser.error("--threshold must be between 0 and 1")
        if not 0 <= args.language_threshold <= 1:
            parser.error("--language-threshold must be between 0 and 1")
        with CrawlerRunLock("filter-maintenance"):
            _filter_command(args)
    elif args.command == "database":
        from database_checkpoint import (
            archive_database,
            database_sync_status,
            restore_database,
        )

        with CrawlerRunLock("database-maintenance"):
            if args.database_command == "archive":
                result = archive_database()
            elif args.database_command == "restore":
                result = restore_database()
            else:
                result = database_sync_status()
        print(json.dumps(result, indent=2))
        if args.database_command == "check" and not result["synchronized"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
