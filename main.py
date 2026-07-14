"""Command-line entry point for the Hometown XR Common Crawl extractor."""

from __future__ import annotations

import argparse
import gc
import logging
import random
import shutil
import signal
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from multiprocessing import Event, current_process

from config import (
    DATA_DIR,
    DB_PATH,
    DEFAULT_CRAWL_ID,
    HARDWARE_PROFILES,
    HEARTBEAT_INTERVAL_SECONDS,
    LANG_DETECTION_THRESHOLD,
    LEASE_TIMEOUT_SECONDS,
    MAX_FILE_ATTEMPTS,
    OUTPUT_DIR,
    SEMANTIC_THRESHOLD,
    HardwareProfile,
    get_hardware_profile,
)
from crawl_catalog import (
    LEGACY_CRAWLS,
    CrawlInfo,
    get_all_crawl_ids,
    get_crawl_info,
    get_modern_crawls,
)
from downloader import fetch_file_paths, stream_file
from output import OutputWriter
from processor import (
    ProcessingStats,
    extract_paragraphs_from_arc,
    extract_paragraphs_from_wet,
)
from progress import ClaimedFile, ProgressTracker
from run_lock import CrawlerRunLock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)-20s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


@dataclass(frozen=True)
class RuntimeSettings:
    profile_name: str
    workers: int
    stream_batch_size: int
    encoding_batch_size: int
    semantic_threshold: float
    language_threshold: float


@dataclass(frozen=True)
class WorkerResult:
    status: str
    records_processed: int = 0
    matches_found: int = 0
    error: str | None = None


_worker_matcher = None
_worker_lang_detector = None
_worker_writer = None
_worker_shutdown_event = None
_shutdown_event = None


def _init_worker(shutdown_event) -> None:
    global _worker_shutdown_event
    _worker_shutdown_event = shutdown_event
    time.sleep(random.uniform(0, 5))

    import warnings

    warnings.filterwarnings("ignore", category=UserWarning)


def _write_match_batch(transaction, batch) -> int:
    matches = _worker_matcher.process_batch_stage2(batch)
    if not matches:
        return 0
    languages = [_worker_lang_detector.detect(match.text) for match in matches]
    transaction.write_matches(matches, languages)
    return len(matches)


def process_file_worker(
    file_path: str,
    crawl_info: CrawlInfo,
    settings: RuntimeSettings,
) -> WorkerResult:
    """Process one complete source, committing output only after full success."""
    global _worker_matcher, _worker_lang_detector, _worker_writer

    shutdown_event = _worker_shutdown_event
    if shutdown_event and shutdown_event.is_set():
        return WorkerResult("interrupted")

    transaction = None
    stats = ProcessingStats()
    try:
        if _worker_matcher is None:
            from language_detector import LanguageDetector
            from matcher import HybridMatcher

            _worker_matcher = HybridMatcher(
                threshold=settings.semantic_threshold,
                encoding_batch_size=settings.encoding_batch_size,
            )
            _worker_lang_detector = LanguageDetector(threshold=settings.language_threshold)
            _worker_writer = OutputWriter()

        logger.info("   Starting: %s...", file_path.split("/")[-1])
        transaction = _worker_writer.begin_source(file_path)
        current_batch = []
        total_matches = 0

        with stream_file(file_path, crawl_info) as stream:
            extractor = (
                extract_paragraphs_from_arc
                if crawl_info.era == "legacy"
                else extract_paragraphs_from_wet
            )
            generator = extractor(
                stream,
                crawl_info.crawl_id,
                _worker_matcher.keyword_matcher,
                shutdown_event,
                stats,
            )
            for paragraph, keyword_matches, _records_seen in generator:
                if shutdown_event and shutdown_event.is_set():
                    stats.interrupted = True
                    break
                current_batch.append((paragraph, keyword_matches))
                if len(current_batch) >= settings.stream_batch_size:
                    total_matches += _write_match_batch(transaction, current_batch)
                    current_batch = []

        if stats.interrupted or (shutdown_event and shutdown_event.is_set()):
            transaction.abort()
            return WorkerResult(
                "interrupted",
                records_processed=stats.records_processed,
                matches_found=total_matches,
            )

        if current_batch:
            total_matches += _write_match_batch(transaction, current_batch)

        transaction.commit()
        return WorkerResult(
            "completed",
            records_processed=stats.records_processed,
            matches_found=total_matches,
        )
    except Exception as exc:
        if transaction is not None:
            transaction.abort()
        return WorkerResult(
            "failed",
            records_processed=stats.records_processed,
            error=f"{type(exc).__name__}: {exc}",
        )


def _signal_handler(signum, frame) -> None:
    del signum, frame
    if _shutdown_event and _shutdown_event.is_set():
        if current_process().name == "MainProcess":
            logger.warning("Second shutdown request received; exiting immediately.")
        raise SystemExit(1)
    if _shutdown_event:
        _shutdown_event.set()
    if current_process().name == "MainProcess":
        logger.info("Shutdown requested. Finishing active workers safely...")


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def _handle_worker_result(
    tracker: ProgressTracker,
    claim: ClaimedFile,
    result: WorkerResult,
) -> tuple[int, int]:
    short_name = claim.file_path.split("/")[-1]
    if result.status == "completed":
        committed = tracker.mark_completed(
            claim.file_path,
            result.records_processed,
            result.matches_found,
            claim.lease_id,
        )
        if not committed:
            logger.error("Lease was lost before completion could be recorded: %s", short_name)
            return 0, 0
        logger.info(
            "   Done: %s (%s records, %s matches)",
            short_name,
            result.records_processed,
            result.matches_found,
        )
        return 1, result.matches_found

    if result.status == "interrupted":
        tracker.release_claim(claim)
        logger.info("   Returned to pending after shutdown: %s", short_name)
        return 0, 0

    error = result.error or "Unknown worker failure"
    tracker.mark_failed(claim.file_path, error, claim.lease_id)
    logger.error("   Failed: %s -> %s", short_name, error)
    return 0, 0


def process_crawl(
    crawl_id: str,
    limit: int | None,
    settings: RuntimeSettings,
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

    files_processed = 0
    matches_found = 0
    submitted = 0
    last_heartbeat = time.monotonic()

    logger.info(
        "Starting %s workers for %s ready files (profile %s)...",
        settings.workers,
        target,
        settings.profile_name,
    )

    with ProcessPoolExecutor(
        max_workers=settings.workers,
        initializer=_init_worker,
        initargs=(_shutdown_event,),
    ) as executor:
        futures = {}

        def submit_available() -> int:
            nonlocal submitted
            if _shutdown_event.is_set() or submitted >= target:
                return 0
            slots = min(settings.workers - len(futures), target - submitted)
            claims = tracker.claim_files(crawl_id, slots, MAX_FILE_ATTEMPTS)
            for claim in claims:
                future = executor.submit(
                    process_file_worker,
                    claim.file_path,
                    crawl_info,
                    settings,
                )
                futures[future] = claim
                submitted += 1
            return len(claims)

        submit_available()
        while futures:
            if _shutdown_event.is_set():
                for future, claim in list(futures.items()):
                    if future.cancel():
                        tracker.release_claim(claim)
                        futures.pop(future)
                if not futures:
                    break

            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                tracker.heartbeat_claims(futures.values())
                last_heartbeat = now

            done, _not_done = wait(
                futures,
                timeout=1.0,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                claim = futures.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = WorkerResult(
                        "failed", error=f"Worker process error: {type(exc).__name__}: {exc}"
                    )
                completed, matches = _handle_worker_result(tracker, claim, result)
                files_processed += completed
                matches_found += matches

            if not _shutdown_event.is_set():
                added = submit_available()
                if not futures and added == 0 and submitted < target:
                    break

    return files_processed, matches_found


def _warmup_models(settings: RuntimeSettings) -> None:
    logger.info("Warming model caches before worker startup...")
    from language_detector import LanguageDetector
    from matcher import HybridMatcher

    matcher = HybridMatcher(
        threshold=settings.semantic_threshold,
        encoding_batch_size=settings.encoding_batch_size,
    )
    detector = LanguageDetector(threshold=settings.language_threshold)
    del matcher, detector
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def run(
    crawl_ids: list[str],
    limit: int | None,
    settings: RuntimeSettings,
) -> None:
    global _shutdown_event
    _shutdown_event = Event()

    with CrawlerRunLock(settings.profile_name):
        OutputWriter().cleanup_stale_staging()
        logger.info("=" * 70)
        logger.info("Hometown XR Common Crawl Extractor")
        logger.info("Crawls: %s", len(crawl_ids))
        logger.info("Profile: %s", settings.profile_name)
        logger.info("Workers: %s", settings.workers)
        logger.info("Semantic threshold: %s", settings.semantic_threshold)
        logger.info("=" * 70)

        _warmup_models(settings)
        total_files = 0
        total_matches = 0
        attempted = 0
        for crawl_id in crawl_ids:
            if _shutdown_event.is_set():
                break
            attempted += 1
            files, matches = process_crawl(crawl_id, limit, settings)
            total_files += files
            total_matches += matches

        logger.info("=" * 70)
        logger.info("Crawls attempted: %s", attempted)
        logger.info("Files completed: %s", total_files)
        logger.info("Matches committed: %s", total_matches)
        logger.info("=" * 70)


def show_status() -> None:
    tracker = ProgressTracker()
    summary = tracker.get_summary()
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

    rows = tracker.get_per_crawl_summary()
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
    scope = crawl_id or "all crawls"
    print(f"Reset {count} failed files for immediate retry in {scope}.")


def recover_leases(minutes: int) -> None:
    with CrawlerRunLock("maintenance"):
        count = ProgressTracker().recover_stale_leases(minutes * 60)
    print(f"Recovered {count} stale processing leases.")


def reset_data() -> None:
    with CrawlerRunLock("maintenance"):
        if DB_PATH.exists():
            DB_PATH.unlink()
        if OUTPUT_DIR.exists():
            shutil.rmtree(OUTPUT_DIR)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        exports_dir = DATA_DIR / "exports"
        if exports_dir.exists():
            shutil.rmtree(exports_dir)
    print("All extracted output and progress have been reset.")


def doctor(profile_name: str) -> int:
    profile = get_hardware_profile(profile_name)
    print("Hometown XR environment check")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  Profile: {profile.name}")
    print(f"  Workers: {profile.workers}")
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


def _runtime_settings(args) -> RuntimeSettings:
    profile: HardwareProfile = get_hardware_profile(args.profile)
    settings = RuntimeSettings(
        profile_name=profile.name,
        workers=args.workers or profile.workers,
        stream_batch_size=args.stream_batch_size or profile.stream_batch_size,
        encoding_batch_size=args.encoding_batch_size or profile.encoding_batch_size,
        semantic_threshold=args.threshold,
        language_threshold=args.language_threshold,
    )
    if settings.workers <= 0:
        raise ValueError("workers must be positive")
    if settings.stream_batch_size <= 0 or settings.encoding_batch_size <= 0:
        raise ValueError("batch sizes must be positive")
    if not 0 <= settings.semantic_threshold <= 1:
        raise ValueError("semantic threshold must be between 0 and 1")
    if not 0 <= settings.language_threshold <= 1:
        raise ValueError("language threshold must be between 0 and 1")
    return settings


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
    run_parser.add_argument("--stream-batch-size", type=int)
    run_parser.add_argument("--encoding-batch-size", type=int)

    subparsers.add_parser("status", help="show processing progress")
    subparsers.add_parser("list", help="list available crawls")
    subparsers.add_parser("reset", help="wipe output and progress")

    retry_parser = subparsers.add_parser("retry", help="retry failed files now")
    retry_parser.add_argument("--crawl")
    retry_parser.add_argument("--all", action="store_true")

    recover_parser = subparsers.add_parser(
        "recover", help="release processing leases older than a threshold"
    )
    recover_parser.add_argument("--minutes", type=int, default=LEASE_TIMEOUT_SECONDS // 60)

    doctor_parser = subparsers.add_parser("doctor", help="check the local runtime")
    doctor_parser.add_argument("--profile", choices=["auto", *HARDWARE_PROFILES], default="auto")

    args = parser.parse_args()
    if args.command == "run":
        if args.limit is not None and args.limit <= 0:
            parser.error("--limit must be positive")
        settings = _runtime_settings(args)
        if args.all:
            crawl_ids = get_all_crawl_ids()
        else:
            crawl_ids = [args.crawl or DEFAULT_CRAWL_ID]
        run(crawl_ids, args.limit, settings)
    elif args.command == "status":
        show_status()
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


if __name__ == "__main__":
    main()
