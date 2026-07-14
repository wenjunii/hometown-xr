"""Bounded CPU parsing pipeline feeding one shared GPU inference service."""

from __future__ import annotations

import logging
import queue
import time
import warnings
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from multiprocessing.context import BaseContext
from typing import Any

from config import HEARTBEAT_INTERVAL_SECONDS, MAX_FILE_ATTEMPTS
from crawl_catalog import CrawlInfo
from downloader import stream_file
from evaluation import DecisionSampler
from metrics import MetricsRecorder
from output import OutputWriter
from processor import ProcessingStats, extract_paragraphs_from_arc, extract_paragraphs_from_wet
from progress import ClaimedFile, ProgressTracker
from record_identity import text_fingerprint
from runtime import RuntimeSettings

logger = logging.getLogger(__name__)
_AUTO_CACHE = object()


@dataclass(frozen=True)
class CandidateBatch:
    source_file: str
    items: list[tuple[Any, list[str]]]


@dataclass(frozen=True)
class SourceFinished:
    source_file: str
    status: str
    records_processed: int = 0
    candidates_found: int = 0
    bytes_read: int = 0
    parse_seconds: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class FinalizedSource:
    source_file: str
    status: str
    records_processed: int
    candidates_found: int
    matches_found: int
    bytes_read: int
    parse_seconds: float
    error: str | None = None


_candidate_queue = None
_parser_shutdown_event = None
_keyword_matcher = None


def init_parser_worker(candidate_queue, shutdown_event) -> None:
    """Initialize a lightweight worker with no CUDA/model state."""
    global _candidate_queue, _parser_shutdown_event
    _candidate_queue = candidate_queue
    _parser_shutdown_event = shutdown_event
    warnings.filterwarnings("ignore", category=UserWarning)


def _put_event(event: CandidateBatch | SourceFinished, final: bool = False) -> bool:
    if _candidate_queue is None:
        raise RuntimeError("parser worker queue is not initialized")
    deadline = time.monotonic() + 10 if final else None
    while True:
        try:
            _candidate_queue.put(event, timeout=0.5)
            return True
        except queue.Full:
            if not final and _parser_shutdown_event and _parser_shutdown_event.is_set():
                return False
            if deadline is not None and time.monotonic() >= deadline:
                return False


def _stream_position(stream) -> int:
    try:
        return max(0, int(stream.tell()))
    except (AttributeError, OSError, TypeError, ValueError):
        return 0


def parse_source_worker(
    file_path: str,
    crawl_info: CrawlInfo,
    candidate_batch_size: int,
) -> SourceFinished:
    """Download, parse, and keyword-filter one source in a CPU process."""
    global _keyword_matcher
    started = time.monotonic()
    stats = ProcessingStats()
    candidates_found = 0
    bytes_read = 0
    status = "completed"
    error = None
    current_batch: list[tuple[Any, list[str]]] = []

    try:
        if _parser_shutdown_event and _parser_shutdown_event.is_set():
            status = "interrupted"
        else:
            if _keyword_matcher is None:
                from matcher import KeywordMatcher

                _keyword_matcher = KeywordMatcher()

            logger.info("   Parsing: %s", file_path.replace("\\", "/").split("/")[-1])
            with stream_file(file_path, crawl_info) as stream:
                extractor = (
                    extract_paragraphs_from_arc
                    if crawl_info.era == "legacy"
                    else extract_paragraphs_from_wet
                )
                generator = extractor(
                    stream,
                    crawl_info.crawl_id,
                    _keyword_matcher,
                    _parser_shutdown_event,
                    stats,
                    file_path,
                )
                for paragraph, keyword_matches, _records_seen in generator:
                    if _parser_shutdown_event and _parser_shutdown_event.is_set():
                        stats.interrupted = True
                        break
                    current_batch.append((paragraph, keyword_matches))
                    if len(current_batch) >= candidate_batch_size:
                        batch = CandidateBatch(file_path, current_batch)
                        if not _put_event(batch):
                            stats.interrupted = True
                            break
                        candidates_found += len(current_batch)
                        current_batch = []
                bytes_read = _stream_position(stream)

            if stats.interrupted or (
                _parser_shutdown_event and _parser_shutdown_event.is_set()
            ):
                status = "interrupted"
            elif current_batch:
                if _put_event(CandidateBatch(file_path, current_batch)):
                    candidates_found += len(current_batch)
                else:
                    status = "interrupted"
    except Exception as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"

    result = SourceFinished(
        source_file=file_path,
        status=status,
        records_processed=stats.records_processed,
        candidates_found=candidates_found,
        bytes_read=bytes_read,
        parse_seconds=time.monotonic() - started,
        error=error,
    )
    _put_event(result, final=True)
    return result


class InferenceService:
    """Own the only semantic model, language detector, and output writer."""

    def __init__(
        self,
        settings: RuntimeSettings,
        metrics: MetricsRecorder,
        matcher=None,
        language_detector=None,
        writer: OutputWriter | None = None,
        sampler: DecisionSampler | None = None,
        cache=_AUTO_CACHE,
    ):
        default_components = matcher is None and language_detector is None
        if matcher is None:
            from matcher import HybridMatcher

            matcher = HybridMatcher(
                threshold=settings.semantic_threshold,
                encoding_batch_size=settings.encoding_batch_size,
                precision=settings.precision,
                adaptive_batching=settings.adaptive_batching,
            )
        if language_detector is None:
            from language_detector import LanguageDetector

            language_detector = LanguageDetector(threshold=settings.language_threshold)
        self.settings = settings
        self.metrics = metrics
        self.matcher = matcher
        self.language_detector = language_detector
        self.writer = writer or OutputWriter()
        self.sampler = sampler or DecisionSampler()
        if cache is _AUTO_CACHE:
            if settings.cache_enabled and default_components:
                from inference_cache import InferenceCache

                try:
                    cache = InferenceCache()
                except Exception as exc:
                    logger.warning(
                        "Inference cache unavailable; continuing uncached: %s",
                        exc,
                    )
                    cache = None
            else:
                cache = None
        self.cache = cache
        self._sampling_language_detector = _CachedLanguageDetector(self)
        self.pending: list[tuple[Any, list[str]]] = []
        self.transactions = {}
        self._closed_order: deque[str] = deque()
        self._closed_sources: set[str] = set()

    def _evaluate_decisions(self, batch: list[tuple[Any, list[str]]]):
        cache_stats = {
            "semantic_cache_hits": 0,
            "semantic_cache_misses": 0,
            "embedding_cache_hits": 0,
            "embedding_cache_misses": 0,
            "semantic_prefiltered": 0,
        }
        cache_capable = self.cache is not None and all(
            hasattr(self.matcher, attribute)
            for attribute in (
                "semantic_cache_namespace",
                "embedding_cache_namespace",
                "score_batch_stage2_with_embeddings",
                "score_cached_embeddings",
                "decisions_from_scores",
            )
        )
        if not cache_capable:
            return self.matcher.evaluate_batch_stage2(batch), cache_stats

        prefiltered = (
            self.matcher.prefilter_semantic_batch(batch)
            if hasattr(self.matcher, "prefilter_semantic_batch")
            else {}
        )
        cache_stats["semantic_prefiltered"] = len(prefiltered)
        active_indexes = [index for index in range(len(batch)) if index not in prefiltered]
        if not active_indexes:
            scores = [prefiltered[index] for index in range(len(batch))]
            return self.matcher.decisions_from_scores(batch, scores), cache_stats
        active_batch = [batch[index] for index in active_indexes]

        hashes = [
            text_fingerprint(paragraph.text) for paragraph, _keywords in active_batch
        ]
        unique_items = {}
        for text_hash, item in zip(hashes, active_batch):
            unique_items.setdefault(text_hash, item)

        score_map = self.cache.get_semantic(
            self.matcher.semantic_cache_namespace,
            unique_items,
        )
        cache_stats["semantic_cache_hits"] = sum(
            text_hash in score_map for text_hash in hashes
        )
        score_misses = [text_hash for text_hash in unique_items if text_hash not in score_map]
        cache_stats["semantic_cache_misses"] = len(score_misses)

        embedding_map = self.cache.get_embeddings(
            self.matcher.embedding_cache_namespace,
            score_misses,
        )
        embedding_hits = [text_hash for text_hash in score_misses if text_hash in embedding_map]
        embedding_misses = [
            text_hash for text_hash in score_misses if text_hash not in embedding_map
        ]
        cache_stats["embedding_cache_hits"] = len(embedding_hits)
        cache_stats["embedding_cache_misses"] = len(embedding_misses)

        newly_scored = {}
        if embedding_hits:
            scores = self.matcher.score_cached_embeddings(
                [embedding_map[text_hash] for text_hash in embedding_hits]
            )
            newly_scored.update(zip(embedding_hits, scores))

        if embedding_misses:
            uncached_batch = [unique_items[text_hash] for text_hash in embedding_misses]
            scores, embeddings = self.matcher.score_batch_stage2_with_embeddings(
                uncached_batch
            )
            newly_scored.update(zip(embedding_misses, scores))
            self.cache.put_embeddings(
                self.matcher.embedding_cache_namespace,
                dict(zip(embedding_misses, embeddings)),
            )

        self.cache.put_semantic(self.matcher.semantic_cache_namespace, newly_scored)
        score_map.update(newly_scored)
        scores = [prefiltered.get(index) for index in range(len(batch))]
        for index, score in zip(
            active_indexes,
            [score_map[text_hash] for text_hash in hashes],
        ):
            scores[index] = score
        decisions = self.matcher.decisions_from_scores(batch, scores)
        return decisions, cache_stats

    def _detect_languages(self, texts: list[str]):
        stats = {"language_cache_hits": 0, "language_cache_misses": 0}
        cache_capable = (
            self.cache is not None
            and hasattr(self.language_detector, "cache_namespace")
            and hasattr(self.language_detector, "predict")
        )
        if not cache_capable:
            return [self.language_detector.detect(text) for text in texts], stats

        hashes = [text_fingerprint(text) for text in texts]
        unique_texts = {}
        for text_hash, value in zip(hashes, texts):
            unique_texts.setdefault(text_hash, value)
        predictions = self.cache.get_languages(
            self.language_detector.cache_namespace,
            unique_texts,
        )
        missing = [text_hash for text_hash in unique_texts if text_hash not in predictions]
        stats["language_cache_hits"] = len(texts) - len(missing)
        stats["language_cache_misses"] = len(missing)
        new_predictions = {
            text_hash: self.language_detector.predict(unique_texts[text_hash])
            for text_hash in missing
        }
        self.cache.put_languages(self.language_detector.cache_namespace, new_predictions)
        predictions.update(new_predictions)
        apply_threshold = getattr(self.language_detector, "apply_threshold", None)
        if apply_threshold is None:
            return [predictions[text_hash] for text_hash in hashes], stats
        return [apply_threshold(predictions[text_hash]) for text_hash in hashes], stats

    def _remember_closed(self, source_file: str) -> None:
        if source_file in self._closed_sources:
            return
        if len(self._closed_order) >= 2048:
            self._closed_sources.discard(self._closed_order.popleft())
        self._closed_order.append(source_file)
        self._closed_sources.add(source_file)

    def _transaction(self, source_file: str):
        transaction = self.transactions.get(source_file)
        if transaction is None:
            transaction = self.writer.begin_source(source_file)
            self.transactions[source_file] = transaction
        return transaction

    def handle_candidate_batch(self, event: CandidateBatch) -> None:
        if event.source_file in self._closed_sources:
            return
        self._transaction(event.source_file)
        self.pending.extend(event.items)
        while len(self.pending) >= self.settings.inference_batch_size:
            batch = self.pending[: self.settings.inference_batch_size]
            del self.pending[: self.settings.inference_batch_size]
            self._infer(batch)

    def _infer(self, batch: list[tuple[Any, list[str]]]) -> None:
        if not batch:
            return
        started = time.monotonic()
        decisions = None
        cache_stats = {}
        if hasattr(self.matcher, "evaluate_batch_stage2"):
            decisions, cache_stats = self._evaluate_decisions(batch)
            matches = [decision.to_match() for decision in decisions if decision.accepted]
        else:
            matches = self.matcher.process_batch_stage2(batch)

        languages, language_cache_stats = self._detect_languages(
            [match.text for match in matches]
        )
        cache_stats.update(language_cache_stats)
        grouped: dict[str, tuple[list, list]] = {}
        fallback_source = batch[0][0].source_file if batch else ""
        for match, language in zip(matches, languages):
            source_file = match.source_file or fallback_source
            match.source_file = source_file
            grouped.setdefault(source_file, ([], []))[0].append(match)
            grouped[source_file][1].append(language)
        for source_file, (source_matches, source_languages) in grouped.items():
            self._transaction(source_file).write_matches(source_matches, source_languages)

        if decisions is not None:
            self.sampler.observe(decisions, self._sampling_language_detector)
        runtime_stats = {}
        semantic_matcher = getattr(self.matcher, "semantic_matcher", None)
        if semantic_matcher is not None and hasattr(
            semantic_matcher, "consume_runtime_stats"
        ):
            runtime_stats = semantic_matcher.consume_runtime_stats()
        self.metrics.record_inference(
            len(batch),
            len(matches),
            time.monotonic() - started,
            cache_stats=cache_stats,
            runtime_stats=runtime_stats,
        )

    def flush(self) -> None:
        if self.pending:
            batch = self.pending
            self.pending = []
            self._infer(batch)

    def finish_source(self, event: SourceFinished) -> FinalizedSource:
        self.flush()
        transaction = self.transactions.pop(event.source_file, None)
        matches_found = 0
        status = event.status
        error = event.error
        try:
            if status == "completed":
                transaction = transaction or self.writer.begin_source(event.source_file)
                matches_found = sum(transaction.commit().values())
            elif transaction is not None:
                transaction.abort()
        except Exception as exc:
            if transaction is not None:
                transaction.abort()
            status = "failed"
            error = f"Output commit failed: {type(exc).__name__}: {exc}"

        self._remember_closed(event.source_file)
        return FinalizedSource(
            source_file=event.source_file,
            status=status,
            records_processed=event.records_processed,
            candidates_found=event.candidates_found,
            matches_found=matches_found,
            bytes_read=event.bytes_read,
            parse_seconds=event.parse_seconds,
            error=error,
        )

    def fail_source(self, source_file: str, error: str) -> FinalizedSource:
        self.pending = [
            item for item in self.pending if item[0].source_file != source_file
        ]
        return self.finish_source(SourceFinished(source_file, "failed", error=error))

    def abort_all(self) -> None:
        self.pending = []
        for transaction in self.transactions.values():
            transaction.abort()
        self.transactions.clear()

    def close(self) -> None:
        self.abort_all()
        if self.cache is not None and hasattr(self.cache, "close"):
            self.cache.close()


class _CachedLanguageDetector:
    """Expose the service cache through the detector interface used by sampling."""

    def __init__(self, service: InferenceService):
        self.service = service

    def detect(self, text: str) -> tuple[str, float]:
        predictions, _stats = self.service._detect_languages([text])
        return predictions[0]


class ExtractionPipeline:
    """Reuse one process pool and one inference service across crawls."""

    def __init__(
        self,
        settings: RuntimeSettings,
        context: BaseContext,
        metrics: MetricsRecorder,
        shutdown_event=None,
        service: InferenceService | None = None,
    ):
        self.settings = settings
        self.context = context
        self.metrics = metrics
        self.shutdown_event = shutdown_event or context.Event()
        self.queue = context.Queue(maxsize=max(8, settings.workers * 4))
        self.service = service or InferenceService(settings, metrics)
        self.executor: ProcessPoolExecutor | None = None

    def __enter__(self) -> "ExtractionPipeline":
        self.executor = ProcessPoolExecutor(
            max_workers=self.settings.workers,
            mp_context=self.context,
            initializer=init_parser_worker,
            initargs=(self.queue, self.shutdown_event),
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)
        self.service.close()
        self.queue.close()
        self.queue.join_thread()

    def _record_finalized(
        self,
        tracker: ProgressTracker,
        claim: ClaimedFile,
        result: FinalizedSource,
    ) -> tuple[int, int]:
        short_name = result.source_file.replace("\\", "/").split("/")[-1]
        completed = 0
        matches = 0
        if result.status == "completed":
            if tracker.mark_completed(
                claim.file_path,
                result.records_processed,
                result.matches_found,
                claim.lease_id,
            ):
                completed = 1
                matches = result.matches_found
                logger.info(
                    "   Done: %s (%s records, %s candidates, %s matches)",
                    short_name,
                    result.records_processed,
                    result.candidates_found,
                    result.matches_found,
                )
            else:
                logger.error("Lease was lost before completion: %s", short_name)
        elif result.status == "interrupted":
            tracker.release_claim(claim)
            logger.info("   Returned to pending after shutdown: %s", short_name)
        else:
            tracker.mark_failed(
                claim.file_path,
                result.error or "Unknown parser failure",
                claim.lease_id,
            )
            logger.error("   Failed: %s -> %s", short_name, result.error)

        self.metrics.record_source(
            result.status,
            result.records_processed,
            result.candidates_found,
            matches,
            result.bytes_read,
            result.parse_seconds,
        )
        return completed, matches

    def process_crawl(
        self,
        tracker: ProgressTracker,
        crawl_info: CrawlInfo,
        target: int,
        max_attempts: int = MAX_FILE_ATTEMPTS,
    ) -> tuple[int, int]:
        if self.executor is None:
            raise RuntimeError("ExtractionPipeline must be used as a context manager")

        futures: dict[Future, ClaimedFile] = {}
        active: dict[str, ClaimedFile] = {}
        fallback_results: dict[str, tuple[SourceFinished, float]] = {}
        submitted = 0
        files_completed = 0
        matches_found = 0
        last_heartbeat = time.monotonic()
        fatal_error = None

        def submit_available() -> None:
            nonlocal submitted
            if self.shutdown_event.is_set() or submitted >= target:
                return
            slots = min(self.settings.workers - len(futures), target - submitted)
            for claim in tracker.claim_files(crawl_info.crawl_id, slots, max_attempts):
                future = self.executor.submit(
                    parse_source_worker,
                    claim.file_path,
                    crawl_info,
                    self.settings.candidate_batch_size,
                )
                futures[future] = claim
                active[claim.file_path] = claim
                submitted += 1

        def finalize(event: SourceFinished) -> None:
            nonlocal files_completed, matches_found
            claim = active.get(event.source_file)
            if claim is None:
                return
            result = self.service.finish_source(event)
            active.pop(event.source_file, None)
            fallback_results.pop(event.source_file, None)
            completed, matches = self._record_finalized(tracker, claim, result)
            files_completed += completed
            matches_found += matches

        def fail_active_sources(exc: Exception) -> None:
            nonlocal fatal_error
            if fatal_error is not None:
                return
            fatal_error = f"Inference service error: {type(exc).__name__}: {exc}"
            logger.exception("Stopping the crawl after an inference service failure")
            self.shutdown_event.set()
            self.service.abort_all()
            fallback_results.clear()
            for source_file, claim in list(active.items()):
                active.pop(source_file, None)
                result = FinalizedSource(
                    source_file=source_file,
                    status="failed",
                    records_processed=0,
                    candidates_found=0,
                    matches_found=0,
                    bytes_read=0,
                    parse_seconds=0.0,
                    error=fatal_error,
                )
                self._record_finalized(tracker, claim, result)

        submit_available()
        while futures or active:
            received = False
            try:
                event = self.queue.get(timeout=0.2)
                received = True
                try:
                    if isinstance(event, CandidateBatch):
                        if event.source_file in active:
                            self.service.handle_candidate_batch(event)
                    elif isinstance(event, SourceFinished):
                        finalize(event)
                except Exception as exc:
                    fail_active_sources(exc)
            except queue.Empty:
                pass

            while True:
                try:
                    event = self.queue.get_nowait()
                except queue.Empty:
                    break
                received = True
                try:
                    if isinstance(event, CandidateBatch):
                        if event.source_file in active:
                            self.service.handle_candidate_batch(event)
                    elif isinstance(event, SourceFinished):
                        finalize(event)
                except Exception as exc:
                    fail_active_sources(exc)

            for future in [future for future in futures if future.done()]:
                claim = futures.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    if claim.file_path in active:
                        failure = self.service.fail_source(
                            claim.file_path,
                            f"Worker process error: {type(exc).__name__}: {exc}",
                        )
                        active.pop(claim.file_path, None)
                        completed, matches = self._record_finalized(tracker, claim, failure)
                        files_completed += completed
                        matches_found += matches
                else:
                    if claim.file_path in active:
                        if result.status == "completed":
                            fallback_results[claim.file_path] = (result, time.monotonic())
                        else:
                            finalize(result)

            now = time.monotonic()
            if not received:
                for source_file, (result, returned_at) in list(fallback_results.items()):
                    if now - returned_at >= 5:
                        logger.warning("Using worker return fallback for %s", source_file)
                        finalize(result)

            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                tracker.heartbeat_claims(active.values())
                self.metrics.flush(force=True)
                last_heartbeat = now

            if self.shutdown_event.is_set():
                for future, claim in list(futures.items()):
                    if future.cancel():
                        futures.pop(future)
                        finalize(SourceFinished(claim.file_path, "interrupted"))
                if not futures and active:
                    for source_file in list(active):
                        finalize(SourceFinished(source_file, "interrupted"))
                continue

            submit_available()
            if not futures and not active and submitted < target:
                break

        return files_completed, matches_found
