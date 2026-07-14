"""Command-line entry point for the Hometown XR Common Crawl extractor."""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import shutil
import signal
import sys

from config import (
    CACHE_DIR,
    DATA_DIR,
    DB_PATH,
    DEFAULT_CRAWL_ID,
    DOMAIN_STORY_CAP,
    HARDWARE_PROFILES,
    LANG_DETECTION_THRESHOLD,
    LEASE_TIMEOUT_SECONDS,
    OUTPUT_DIR,
    PARQUET_DIR,
    SEMANTIC_THRESHOLD,
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
from metrics import MetricsRecorder, print_latest
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


def _signal_handler(signum, frame) -> None:
    del signum, frame
    if _shutdown_event and _shutdown_event.is_set():
        logger.warning("Second shutdown request received; exiting immediately.")
        raise SystemExit(1)
    if _shutdown_event:
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
) -> tuple[int, int]:
    crawl_info = get_crawl_info(crawl_id)
    format_name = "ARC (HTML)" if crawl_info.era == "legacy" else "WET (text)"
    logger.info("--- Crawl: %s [%s] ---", crawl_id, format_name)

    tracker = ProgressTracker()
    tracker.recover_stale_leases(LEASE_TIMEOUT_SECONDS)
    logger.info("Fetching file list...")
    file_paths = fetch_file_paths(crawl_info)
    if not file_paths:
        logger.warning("No files found for %s. Skipping.", crawl_id)
        return 0, 0

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
        return 0, 0

    pipeline.metrics.add_target_files(target)
    logger.info(
        "Running %s CPU parsers into one GPU inference service (%s sources)...",
        settings.workers,
        target,
    )
    return pipeline.process_crawl(tracker, crawl_info, target)


def run(crawl_ids: list[str], limit: int | None, settings: RuntimeSettings) -> None:
    global _shutdown_event
    context = multiprocessing.get_context("spawn")
    _shutdown_event = context.Event()
    metrics = MetricsRecorder(
        profile=settings.profile_name,
        workers=settings.workers,
        inference_batch_size=settings.inference_batch_size,
        gpu_name=_gpu_name(),
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
            logger.info("=" * 70)

            total_files = 0
            total_matches = 0
            attempted = 0
            with ExtractionPipeline(
                settings,
                context,
                metrics,
                shutdown_event=_shutdown_event,
            ) as pipeline:
                for crawl_id in crawl_ids:
                    if _shutdown_event.is_set():
                        break
                    attempted += 1
                    files, matches = process_crawl(crawl_id, limit, settings, pipeline)
                    total_files += files
                    total_matches += matches

            logger.info("=" * 70)
            logger.info("Crawls attempted: %s", attempted)
            logger.info("Files completed: %s", total_files)
            logger.info("Matches committed: %s", total_matches)
            logger.info("=" * 70)
    finally:
        metrics.close()
        _shutdown_event = None


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


def retry_failed(crawl_id: str | None) -> None:
    with CrawlerRunLock("maintenance"):
        count = ProgressTracker().retry_failed(crawl_id)
    print(f"Reset {count} failed files for immediate retry in {crawl_id or 'all crawls'}.")


def recover_leases(minutes: int) -> None:
    with CrawlerRunLock("maintenance"):
        count = ProgressTracker().recover_stale_leases(minutes * 60)
    print(f"Recovered {count} stale processing leases.")


def reset_data() -> None:
    with CrawlerRunLock("maintenance"):
        if DB_PATH.exists():
            DB_PATH.unlink()
        for directory in (OUTPUT_DIR, DATA_DIR / "exports", PARQUET_DIR, CACHE_DIR):
            if directory.exists():
                shutil.rmtree(directory)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("All extracted output, derivatives, and progress have been reset.")


def doctor(profile_name: str) -> int:
    profile = get_hardware_profile(profile_name)
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
        print(f"  CUDA available: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"  GPU: {torch.cuda.get_device_name(0)}")
    except ImportError:
        print("  PyTorch: missing")
        return 1
    return 0


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
    return settings


def _evaluation_command(args) -> None:
    from evaluation import annotate, build_annotation_sample, evaluation_report

    if args.evaluation_command == "sample":
        print(json.dumps(build_annotation_sample(size=args.size), indent=2))
    elif args.evaluation_command == "annotate":
        print(
            json.dumps(
                annotate(language=args.language, limit=args.limit),
                indent=2,
            )
        )
    elif args.evaluation_command == "report":
        print(json.dumps(evaluation_report(), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract personal home and belonging narratives from Common Crawl"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="start or resume processing")
    run_parser.add_argument("--crawl")
    run_parser.add_argument("--all", action="store_true")
    run_parser.add_argument("--limit", type=int)
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
    subparsers.add_parser("metrics", help="show the latest operational metrics")
    subparsers.add_parser("list", help="list available crawls")
    subparsers.add_parser("reset", help="wipe output and progress")
    subparsers.add_parser("verify-output", help="verify source shard checksums")

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
    retry_parser.add_argument("--crawl")
    retry_parser.add_argument("--all", action="store_true")

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
    evaluation_subparsers.add_parser("report")

    args = parser.parse_args()
    if args.command == "run":
        if args.limit is not None and args.limit <= 0:
            parser.error("--limit must be positive")
        settings = _runtime_settings(args)
        crawl_ids = get_all_crawl_ids() if args.all else [args.crawl or DEFAULT_CRAWL_ID]
        run(crawl_ids, args.limit, settings)
    elif args.command == "status":
        show_status()
    elif args.command == "metrics":
        print_latest()
    elif args.command == "list":
        list_crawls()
    elif args.command == "retry":
        retry_failed(None if args.all else (args.crawl or DEFAULT_CRAWL_ID))
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
        from benchmark import run_benchmark

        with CrawlerRunLock("benchmark"):
            print(
                json.dumps(
                    run_benchmark(args.profile, quick=args.quick, write=not args.no_write),
                    indent=2,
                )
            )
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
        _evaluation_command(args)


if __name__ == "__main__":
    main()
