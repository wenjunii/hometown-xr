"""
Common Crawl Home/Belonging Extractor - CLI Entry Point

A resumable application that streams Common Crawl WET/ARC files, detects
language, and extracts paragraphs semantically related to concepts of "home",
"hometown", "belonging", "roots", "childhood", etc. - across all languages.

Supports all Common Crawl datasets from 2008 to present:
  - Modern crawls (2013+): WET format (pre-extracted text)
  - Legacy crawls (2008-2012): ARC format (HTML -> text extraction)

Usage:
    python main.py run --crawl CC-MAIN-2026-12           # Process one crawl
    python main.py run --crawl CC-MAIN-2026-12 --limit 5 # Test with 5 files
    python main.py run --all                              # Process ALL crawls
    python main.py status                                 # Show progress
    python main.py list                                   # List all crawls
"""

import argparse
import logging
import signal
import os
import random
import re
import sys
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, as_completed, FIRST_COMPLETED, wait
from multiprocessing import Event, current_process

from config import (
    DEFAULT_CRAWL_ID, SEMANTIC_THRESHOLD, MAX_WORKERS, 
    MAX_PARAGRAPHS_PER_BATCH, DB_PATH, OUTPUT_DIR, DATA_DIR
)
from crawl_catalog import CrawlInfo, get_crawl_info, get_all_crawl_ids, is_legacy_crawl, get_modern_crawls, LEGACY_CRAWLS
from downloader import fetch_file_paths, stream_file
from processor import extract_paragraphs_from_wet, extract_paragraphs_from_arc
from progress import ProgressTracker
from output import OutputWriter

# -- Logging Setup ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)-20s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

# Global variables in worker processes (initialized lazily)
_worker_matcher = None
_worker_lang_detector = None
_worker_writer = None
_worker_shutdown_event = None


def _init_worker(shutdown_event):
    """Initialize global state in worker processes."""
    global _worker_shutdown_event
    _worker_shutdown_event = shutdown_event
    
    # Stagger initialization to prevent VRAM spikes on Windows
    # (prevents all workers from grabbing 700MB at once)
    time.sleep(random.uniform(0, 5))
    
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning)


def process_file_worker(
    file_path: str, crawl_info: CrawlInfo, threshold: float
) -> tuple[int, int, str | None]:
    """
    Process a single file (called by ProcessPoolExecutor).
    Returns (records_processed, matches_found, error_msg)
    """
    # Use the global event initialized by _init_worker
    global _worker_shutdown_event
    shutdown_event = _worker_shutdown_event

    if shutdown_event and shutdown_event.is_set():
        return 0, 0, "Skipped due to shutdown"

    short_name = file_path.split("/")[-1]
    logger.info(f"   Starting: {short_name}...")
    
    global _worker_matcher, _worker_lang_detector, _worker_writer

    try:
        # Lazy initialization of ML models in the worker process
        if _worker_matcher is None:
            # Import inside worker to avoid overhead in parent/other processes
            from matcher import HybridMatcher
            from language_detector import LanguageDetector
            _worker_matcher = HybridMatcher(threshold=threshold)
            _worker_lang_detector = LanguageDetector()
            _worker_writer = OutputWriter()

        is_legacy = crawl_info.era == "legacy"

        # Stream and parse the file using the generator-based pipeline
        stream = stream_file(file_path, crawl_info)
        
        # Accumulate batches for the GPU
        # Smaller batch size (200 instead of 500) makes GPU load "smoother" 
        # and reduces VRAM peaks that can kill processes on Windows.
        STREAM_BATCH_SIZE = 200
        current_batch = []
        total_matches = 0
        total_records_processed = 0

        if is_legacy:
            generator = extract_paragraphs_from_arc(stream, crawl_info.crawl_id, _worker_matcher.keyword_matcher, shutdown_event)
        else:
            generator = extract_paragraphs_from_wet(stream, crawl_info.crawl_id, _worker_matcher.keyword_matcher, shutdown_event)

        for para, kw_matches, records_seen in generator:
            if shutdown_event and shutdown_event.is_set():
                break

            total_records_processed = records_seen
            current_batch.append((para, kw_matches))

            # When batch is full, send to GPU for matching
            if len(current_batch) >= STREAM_BATCH_SIZE:
                matches = _worker_matcher.process_batch_stage2(current_batch)
                if matches:
                    total_matches += len(matches)
                    languages = [_worker_lang_detector.detect(m.text) for m in matches]
                    _worker_writer.write_matches(matches, languages, file_path)
                current_batch = []

        # Process final partial batch (if not skipped by shutdown)
        if current_batch and not (shutdown_event and shutdown_event.is_set()):
            matches = _worker_matcher.process_batch_stage2(current_batch)
            if matches:
                total_matches += len(matches)
                languages = [_worker_lang_detector.detect(m.text) for m in matches]
                _worker_writer.write_matches(matches, languages, file_path)

        return total_records_processed, total_matches, None

    except Exception as e:
        return 0, 0, str(e)


_shutdown_event = None


def _signal_handler(signum, frame):
    global _shutdown_event
    if _shutdown_event and _shutdown_event.is_set():
        if current_process().name == "MainProcess":
            logger.warning("Force quit! Exiting immediately.")
        sys.exit(1)
    
    if _shutdown_event:
        _shutdown_event.set()
    
    # Only the main process should log the shutdown request to avoid noise
    if current_process().name == "MainProcess":
        logger.info("Shutdown requested. Cleaning up...")


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# -- Process a Single Crawl ---------------------------------------------------

def process_crawl(
    crawl_id: str,
    limit: int | None,
    threshold: float,
) -> tuple[int, int]:
    """
    Process a single crawl using a pool of workers.
    """
    global _shutdown_requested

    # Fetch crawl metadata once in the parent process
    # This prevents all workers from hitting the network simultaneously
    crawl_info = get_crawl_info(crawl_id)
    is_legacy = crawl_info.era == "legacy"

    logger.info(f"--- Crawl: {crawl_id} [{'ARC (HTML)' if is_legacy else 'WET (text)'}] ---")

    tracker = ProgressTracker()

    # Fetch file list
    logger.info("Fetching file list...")
    file_paths = fetch_file_paths(crawl_info)

    if not file_paths:
        logger.warning(f"No files found for {crawl_id}. Skipping.")
        return 0, 0

    # Initialize progress tracking (scoped to this crawl)
    tracker.initialize_paths(file_paths, crawl_id)
    summary = tracker.get_summary(crawl_id)
    logger.info(
        f"Progress: {summary['completed']}/{summary['total_files']} completed, "
        f"{summary['total_matches']} matches so far"
    )

    # Filtering down to files we actually need to process
    pending_files = []
    total_to_process = limit if limit else summary['pending']
    
    if total_to_process > 0:
        logger.info(f"Preparing {total_to_process} tasks in batches...")
        
        batch_size = 5000
        while len(pending_files) < total_to_process:
            needed = total_to_process - len(pending_files)
            current_batch_size = min(batch_size, needed)
            
            batch = tracker.get_batch_pending(crawl_id, limit=current_batch_size)
            if not batch:
                break
                
            tracker.mark_batch_processing(batch)
            pending_files.extend(batch)
            
            if len(pending_files) % 10000 == 0 or len(pending_files) == total_to_process:
                logger.info(f"   ... ready {len(pending_files)}/{total_to_process}")

    if not pending_files:
        logger.info(f"No pending files to process for {crawl_id}.")
        return 0, 0

    files_processed = 0
    matches_found = 0

    # Execute in parallel with throttled submission
    logger.info(f"Starting parallel processing pool with {MAX_WORKERS} workers...")
    
    with ProcessPoolExecutor(
        max_workers=MAX_WORKERS, 
        initializer=_init_worker, 
        initargs=(_shutdown_event,)
    ) as executor:
        futures = {}
        pending_iter = iter(pending_files)
        
        # Buffer size: keep enough tasks to keep all workers busy
        max_active = MAX_WORKERS * 2
        
        try:
            # Initial submission
            for _ in range(min(len(pending_files), max_active)):
                f_path = next(pending_iter)
                fut = executor.submit(process_file_worker, f_path, crawl_info, threshold)
                futures[fut] = f_path

            while futures:
                if _shutdown_event.is_set():
                    logger.info("Cancelling pending tasks...")
                    # For Python 3.9+ we can cancel_futures
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                # Wait for at least one task to complete
                done, _ = wait(futures.keys(), return_when=FIRST_COMPLETED, timeout=1.0)
                
                for future in done:
                    file_path = futures.pop(future)
                    short_name = file_path.split("/")[-1]
                    
                    try:
                        records, matches, error = future.result()
                        if error:
                            if error == "Skipped due to shutdown":
                                tracker.mark_batch_processing([file_path]) # Reset it? No, just leave it as processing for now or mark failed.
                                # Actually tracker already marked it as processing. We should probably reset it or mark it for resume.
                                logger.debug(f"   Skipped: {short_name} (shutdown)")
                            else:
                                logger.error(f"   Failed: {short_name} -> {error}")
                                tracker.mark_failed(file_path, error)
                        else:
                            tracker.mark_completed(file_path, records, matches)
                            files_processed += 1
                            matches_found += matches
                            logger.info(f"   Done: {short_name} ({records} records, {matches} matches)")

                    except Exception as e:
                        logger.error(f"   Critical error in worker for {short_name}: {e}")
                        tracker.mark_failed(file_path, str(e))

                # Refill the buffer
                while len(futures) < max_active and not _shutdown_event.is_set():
                    try:
                        f_path = next(pending_iter)
                        fut = executor.submit(process_file_worker, f_path, crawl_info, threshold)
                        futures[fut] = f_path
                    except StopIteration:
                        break

        except KeyboardInterrupt:
            if _shutdown_event:
                _shutdown_event.set()
            executor.shutdown(wait=False, cancel_futures=True)

    return files_processed, matches_found


def _warmup_models(threshold: float):
    """Ensure models are downloaded and cached before workers start."""
    logger.info("Warming up ML models (ensures cache is ready)...")
    from matcher import HybridMatcher
    from language_detector import LanguageDetector
    HybridMatcher(threshold=threshold)
    LanguageDetector()
    logger.info("Models warmed up and cached.")


# -- Main Commands ------------------------------------------------------------

def run(crawl_ids: list[str], limit: int | None, threshold: float):
    """Main processing loop for one or more crawls."""
    global _shutdown_event

    # Initialize shared shutdown event (using standard Event instead of Manager to avoid pipe errors)
    _shutdown_event = Event()

    logger.info("=" * 70)
    logger.info("  Common Crawl Home/Belonging Extractor (Parallel GPU Mode)")
    logger.info(f"  Crawls to process: {len(crawl_ids)}")
    logger.info(f"  Semantic threshold: {threshold}")
    logger.info(f"  Max workers:        {MAX_WORKERS}")
    if limit:
        logger.info(f"  File limit per crawl: {limit}")
    logger.info("=" * 70)

    # Ensure models are cached before workers start
    _warmup_models(threshold)

    logger.info("Starting processing loop. Tasks will be distributed to workers.\n")

    total_files = 0
    total_matches = 0

    try:
        for i, crawl_id in enumerate(crawl_ids):
            if _shutdown_event.is_set():
                break

            logger.info(f"\n[{i+1}/{len(crawl_ids)}] Starting crawl: {crawl_id}")
            files, matches = process_crawl(
                crawl_id, limit, threshold
            )
            total_files += files
            total_matches += matches
    finally:
        # Final summary
        logger.info("")
        logger.info("=" * 70)
        logger.info(f"  Session Summary")
        logger.info(f"  Crawls attempted:       {min(i+1, len(crawl_ids)) if crawl_ids else 0}")
        logger.info(f"  Files processed:        {total_files}")
        logger.info(f"  Matches found:          {total_matches}")
        logger.info("=" * 70)


def show_status():
    """Show current processing status across all crawls."""
    tracker = ProgressTracker()
    summary = tracker.get_summary()

    print("\n" + "=" * 60)
    print("  CC Home/Belonging Extractor - Overall Status")
    print("=" * 60)
    print(f"  Total files:       {summary['total_files']}")
    print(f"  Completed:         {summary['completed']}")
    print(f"  Pending:           {summary['pending']}")
    print(f"  Processing:        {summary['processing']}")
    print(f"  Failed:            {summary['failed']}")
    print(f"  Progress:          {summary['progress_pct']:.2f}%")
    print(f"  ----------------------")
    print(f"  Records processed: {summary['total_records']:,}")
    print(f"  Matches found:     {summary['total_matches']:,}")
    print("=" * 60)

    # Per-crawl breakdown
    crawl_summaries = tracker.get_per_crawl_summary()
    if crawl_summaries:
        print("\n  Per-Crawl Breakdown:")
        print(f"  {'Crawl ID':<25} {'Done':>8} {'Total':>8} {'Matches':>10} {'Status':>10}")
        print(f"  {'-'*25} {'-'*8} {'-'*8} {'-'*10} {'-'*10}")
        for cs in crawl_summaries:
            status = "DONE" if cs['completed'] == cs['total'] else "IN PROGRESS"
            print(
                f"  {cs['crawl_id']:<25} "
                f"{cs['completed']:>8} "
                f"{cs['total']:>8} "
                f"{cs['matches']:>10} "
                f"{status:>10}"
            )
    print()


def list_crawls():
    """List all available crawls."""
    print("\n" + "=" * 60)
    print("  Available Common Crawl Datasets")
    print("=" * 60)

    print("\n  LEGACY CRAWLS (2008-2012) - ARC format (HTML)")
    print(f"  {'-'*55}")
    for crawl in LEGACY_CRAWLS:
        print(f"  {crawl.crawl_id:<25} {crawl.notes}")

    modern_crawls = get_modern_crawls()
    print(f"\n  MODERN CRAWLS (2013-present) - WET format (text)")
    print(f"  (auto-discovered from Common Crawl index API)")
    print(f"  {'-'*55}")
    # Group by year
    current_year = None
    for crawl_id in reversed(modern_crawls):
        year = crawl_id.split("-")[2]
        if year != current_year:
            current_year = year
            print(f"\n  {year}:")
        print(f"    {crawl_id}")

    total = len(LEGACY_CRAWLS) + len(modern_crawls)
    print(f"\n  Total: {total} crawls available")
    print(f"  New crawls are auto-discovered when published by Common Crawl.")
    print(f"  Use: python main.py run --crawl <ID>")
    print(f"  Or:  python main.py run --all\n")


def reset_data():
    """Wipe all extracted matches and reset crawl progress."""
    print("\n" + "=" * 60)
    print("  WIPING ALL EXTRACTED DATA AND PROGRESS")
    print("=" * 60)
    
    # 1. Reset Progress Database
    if DB_PATH.exists():
        print(f"  [*] Deleting progress database: {DB_PATH.name}")
        try:
            DB_PATH.unlink()
        except Exception as e:
            print(f"      Error: {e}")

    # 2. Clear Output Directory (Matches)
    if OUTPUT_DIR.exists():
        print(f"  [*] Clearing output match files from: {OUTPUT_DIR.name}")
        # We keep the language directories but delete their contents
        for item in OUTPUT_DIR.iterdir():
            if item.is_dir():
                try:
                    shutil.rmtree(item)
                except Exception as e:
                    print(f"      Error clearing {item.name}: {e}")
            else:
                try:
                    item.unlink()
                except Exception as e:
                    print(f"      Error deleting {item.name}: {e}")
        # Re-create the structure if needed (though run() does this)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 3. Clear Exports
    exports_dir = DATA_DIR / "exports"
    if exports_dir.exists():
        print(f"  [*] Clearing exports: {exports_dir.name}")
        try:
            shutil.rmtree(exports_dir)
            exports_dir.mkdir(exist_ok=True)
        except Exception as e:
            print(f"      Error: {e}")

    print("\n  Reset complete. Workspace is clean.")
    print("  Run 'python main.py run --all' to start fresh.\n")


# -- CLI Argument Parsing -----------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Extract home/belonging paragraphs from Common Crawl datasets (2008-present)"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Run command
    run_parser = subparsers.add_parser("run", help="Start or resume processing")
    run_parser.add_argument(
        "--crawl",
        default=None,
        help=f"Crawl ID to process (default: {DEFAULT_CRAWL_ID})",
    )
    run_parser.add_argument(
        "--all",
        action="store_true",
        help="Process ALL crawls from 2008 to present",
    )
    run_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of files to process per crawl (for testing)",
    )
    run_parser.add_argument(
        "--threshold",
        type=float,
        default=SEMANTIC_THRESHOLD,
        help=f"Semantic similarity threshold (default: {SEMANTIC_THRESHOLD})",
    )

    # Status command
    subparsers.add_parser("status", help="Show processing progress")

    # List command
    subparsers.add_parser("list", help="List all available crawls")

    # Reset command
    subparsers.add_parser("reset", help="Wipe all data and start fresh")

    args = parser.parse_args()

    if args.command == "run":
        if args.all:
            crawl_ids = get_all_crawl_ids()
        elif args.crawl:
            crawl_ids = [args.crawl]
        else:
            crawl_ids = [DEFAULT_CRAWL_ID]
        run(crawl_ids, args.limit, args.threshold)
    elif args.command == "status":
        show_status()
    elif args.command == "list":
        list_crawls()
    elif args.command == "reset":
        reset_data()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
