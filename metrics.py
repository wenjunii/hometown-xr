"""Lightweight operational metrics for long extractor runs."""

from __future__ import annotations

import gzip
import io
import json
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config import METRICS_DIR, METRICS_FLUSH_SECONDS, RUN_HISTORY_PATH
from deterministic_gzip import gzip_binary_writer
from failure_analysis import classify_failure


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class MetricsRecorder:
    """Accumulate run counters and periodically publish a local snapshot."""

    def __init__(
        self,
        profile: str,
        workers: int,
        inference_batch_size: int,
        metrics_dir: str | Path = METRICS_DIR,
        gpu_name: str = "unknown",
        provenance: dict | None = None,
    ):
        self.metrics_dir = Path(metrics_dir)
        self.provenance = dict(provenance or {})
        self.session_id = str(
            self.provenance.get("run_id")
            or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        )
        self.started_at = _utc_now()
        self._started_monotonic = time.monotonic()
        self._last_flush = 0.0
        self.profile = profile
        self.workers = workers
        self.inference_batch_size = inference_batch_size
        self.gpu_name = gpu_name
        self.target_files = 0
        self.encoding_batch_size = 0
        self.counters: dict[str, float] = {
            "files_completed": 0,
            "files_failed": 0,
            "files_interrupted": 0,
            "records_processed": 0,
            "eligible_paragraphs": 0,
            "keyword_rejected": 0,
            "keyword_candidates": 0,
            "matches_accepted": 0,
            "matches_committed": 0,
            "bytes_read": 0,
            "parse_seconds": 0.0,
            "inference_seconds": 0.0,
            "inference_batches": 0,
            "semantic_cache_hits": 0,
            "semantic_cache_misses": 0,
            "embedding_cache_hits": 0,
            "embedding_cache_misses": 0,
            "semantic_prefiltered": 0,
            "language_cache_hits": 0,
            "language_cache_misses": 0,
            "cuda_oom_retries": 0,
            "batch_reductions": 0,
            "process_pool_restarts": 0,
            "process_pool_recycles": 0,
            "source_cooldowns": 0,
            "source_cooldown_seconds": 0.0,
            "peak_worker_rss_bytes": 0,
            "peak_vram_mb": 0.0,
        }
        self.failure_categories: dict[str, int] = {}

    def add_target_files(self, count: int) -> None:
        self.target_files += max(0, count)
        self.flush()

    def record_inference(
        self,
        candidates: int,
        matches: int,
        seconds: float,
        cache_stats: dict[str, int] | None = None,
        runtime_stats: dict[str, int | float] | None = None,
    ) -> None:
        self.counters["keyword_candidates"] += candidates
        self.counters["matches_accepted"] += matches
        self.counters["inference_seconds"] += seconds
        self.counters["inference_batches"] += 1
        for key, value in (cache_stats or {}).items():
            if key in self.counters:
                self.counters[key] += value
        runtime_stats = runtime_stats or {}
        self.counters["cuda_oom_retries"] += runtime_stats.get("oom_retries", 0)
        self.counters["batch_reductions"] += runtime_stats.get("batch_reductions", 0)
        if runtime_stats.get("encoding_batch_size"):
            self.encoding_batch_size = int(runtime_stats["encoding_batch_size"])
        self.counters["peak_vram_mb"] = max(
            self.counters["peak_vram_mb"],
            float(runtime_stats.get("peak_vram_mb", 0.0)),
        )
        self.flush()

    def record_source(
        self,
        status: str,
        records: int,
        candidates: int,
        matches: int,
        bytes_read: int,
        parse_seconds: float,
        eligible_paragraphs: int = 0,
        keyword_rejected: int = 0,
        peak_worker_rss_bytes: int = 0,
        error: str | None = None,
    ) -> None:
        key = f"files_{status}"
        if key in self.counters:
            self.counters[key] += 1
        self.counters["records_processed"] += records
        self.counters["eligible_paragraphs"] += eligible_paragraphs
        self.counters["keyword_rejected"] += keyword_rejected
        self.counters["matches_committed"] += matches
        self.counters["bytes_read"] += bytes_read
        self.counters["parse_seconds"] += parse_seconds
        self.counters["peak_worker_rss_bytes"] = max(
            self.counters["peak_worker_rss_bytes"],
            peak_worker_rss_bytes,
        )
        if status == "failed":
            category = classify_failure(error)
            self.failure_categories[category] = self.failure_categories.get(category, 0) + 1
        # Candidates are counted by inference batches, including batches that
        # span multiple sources, so they are intentionally not added here.
        del candidates
        self.flush()

    def record_pool_restart(self) -> None:
        self.counters["process_pool_restarts"] += 1
        self.flush(force=True)

    def record_pool_recycle(self) -> None:
        self.counters["process_pool_recycles"] += 1
        self.flush(force=True)

    def record_source_cooldown(self, seconds: float) -> None:
        self.counters["source_cooldowns"] += 1
        self.counters["source_cooldown_seconds"] += max(0.0, seconds)
        self.flush(force=True)

    def snapshot(self, final: bool = False) -> dict:
        elapsed = max(time.monotonic() - self._started_monotonic, 1e-9)
        finished = int(
            self.counters["files_completed"]
            + self.counters["files_failed"]
            + self.counters["files_interrupted"]
        )
        rate = finished / elapsed
        remaining = max(self.target_files - finished, 0)
        eta = remaining / rate if rate > 0 else None
        payload = {
            "schema_version": 3,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "updated_at": _utc_now(),
            "final": final,
            "profile": self.profile,
            "gpu": self.gpu_name,
            "workers": self.workers,
            "inference_batch_size": self.inference_batch_size,
            "encoding_batch_size": self.encoding_batch_size or None,
            "target_files": self.target_files,
            "elapsed_seconds": round(elapsed, 3),
            **{
                key: round(value, 3) if isinstance(value, float) else value
                for key, value in self.counters.items()
            },
            "rates": {
                "files_per_hour": round(rate * 3600, 3),
                "records_per_second": round(self.counters["records_processed"] / elapsed, 3),
                "candidates_per_second": round(
                    self.counters["keyword_candidates"] / elapsed, 3
                ),
                "matches_per_hour": round(
                    self.counters["matches_committed"] / elapsed * 3600, 3
                ),
                "megabytes_per_second": round(
                    self.counters["bytes_read"] / 1_000_000 / elapsed, 3
                ),
                "semantic_cache_hit_rate": round(
                    self.counters["semantic_cache_hits"]
                    / max(
                        self.counters["semantic_cache_hits"]
                        + self.counters["semantic_cache_misses"],
                        1,
                    ),
                    4,
                ),
            },
            "eta_seconds": round(eta, 1) if eta is not None else None,
            "provenance": self.provenance,
        }
        payload["acceptance_funnel"] = {
            "eligible_paragraphs": int(self.counters["eligible_paragraphs"]),
            "keyword_rejected": int(self.counters["keyword_rejected"]),
            "keyword_candidates": int(self.counters["keyword_candidates"]),
            "filter_accepted": int(self.counters["matches_accepted"]),
            "committed": int(self.counters["matches_committed"]),
            "filter_rejected": max(
                0,
                int(self.counters["keyword_candidates"])
                - int(self.counters["matches_accepted"]),
            ),
        }
        payload["failure_categories"] = dict(sorted(self.failure_categories.items()))
        payload["resources"] = {
            "peak_worker_rss_mb": round(
                self.counters["peak_worker_rss_bytes"] / 1024**2,
                1,
            ),
            "peak_vram_mb": round(float(self.counters["peak_vram_mb"]), 1),
        }
        return payload

    def flush(self, force: bool = False, final: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_flush < METRICS_FLUSH_SECONDS:
            return
        payload = self.snapshot(final=final)
        _atomic_json(self.metrics_dir / "latest.json", payload)
        _atomic_json(self.metrics_dir / "sessions" / f"{self.session_id}.json", payload)
        self._last_flush = now

    def close(self) -> dict:
        self.flush(force=True, final=True)
        payload = self.snapshot(final=True)
        self.metrics_dir.mkdir(parents=True, exist_ok=True)
        with (self.metrics_dir / "history.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        return payload


def compact_run_history(
    metrics_dir: str | Path = METRICS_DIR,
    target_path: str | Path = RUN_HISTORY_PATH,
    max_runs: int = 1_000,
) -> dict:
    """Merge machine-local run metrics into a deterministic shared history."""
    if max_runs <= 0:
        raise ValueError("max_runs must be positive")
    target = Path(target_path)
    rows: dict[str, dict] = {}
    if target.exists():
        with gzip.open(target, "rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    rows[str(row.get("session_id", ""))] = row

    local_path = Path(metrics_dir) / "history.jsonl"
    if local_path.exists():
        with local_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    rows[str(row.get("session_id", ""))] = row

    ordered = sorted(
        rows.values(),
        key=lambda row: (str(row.get("started_at", "")), str(row.get("session_id", ""))),
    )[-max_runs:]
    before = target.stat().st_size if target.exists() else 0
    if ordered:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("wb") as raw:
            with gzip_binary_writer(raw) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8") as handle:
                    for row in ordered:
                        handle.write(json.dumps(row, sort_keys=True) + "\n")
        os.replace(temporary, target)
    return {
        "runs": len(ordered),
        "bytes_before": before,
        "bytes_after": target.stat().st_size if target.exists() else 0,
    }


def latest_metrics(metrics_dir: str | Path = METRICS_DIR) -> dict | None:
    path = Path(metrics_dir) / "latest.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def concise_metrics(payload: dict) -> dict:
    """Remove bulky provenance contracts while retaining actionable run health."""
    provenance = payload.get("provenance") or {}
    return {
        "schema_version": payload.get("schema_version"),
        "session_id": payload.get("session_id"),
        "started_at": payload.get("started_at"),
        "final": payload.get("final"),
        "profile": payload.get("profile"),
        "gpu": payload.get("gpu"),
        "workers": payload.get("workers"),
        "target_files": payload.get("target_files"),
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "eta_seconds": payload.get("eta_seconds"),
        "files": {
            "completed": payload.get("files_completed", 0),
            "failed": payload.get("files_failed", 0),
            "interrupted": payload.get("files_interrupted", 0),
        },
        "rates": payload.get("rates", {}),
        "acceptance_funnel": payload.get("acceptance_funnel", {}),
        "failure_categories": payload.get("failure_categories", {}),
        "resources": payload.get("resources", {}),
        "process_pool_restarts": payload.get("process_pool_restarts", 0),
        "process_pool_recycles": payload.get("process_pool_recycles", 0),
        "source_cooldowns": payload.get("source_cooldowns", 0),
        "source_cooldown_seconds": payload.get("source_cooldown_seconds", 0),
        "cuda_oom_retries": payload.get("cuda_oom_retries", 0),
        "filter_signature": provenance.get("filter_signature"),
        "run_mode": provenance.get("mode", "crawl"),
    }


def _history_rows(
    metrics_dir: str | Path = METRICS_DIR,
    history_path: str | Path = RUN_HISTORY_PATH,
) -> list[dict]:
    rows: dict[str, dict] = {}
    shared = Path(history_path)
    if shared.exists():
        with gzip.open(shared, "rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    rows[str(row.get("session_id", ""))] = row
    local = Path(metrics_dir) / "history.jsonl"
    if local.exists():
        with local.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    rows[str(row.get("session_id", ""))] = row
    return sorted(
        rows.values(),
        key=lambda row: (str(row.get("started_at", "")), str(row.get("session_id", ""))),
    )


def summarize_run_history(
    limit: int = 20,
    profile: str | None = None,
    metrics_dir: str | Path = METRICS_DIR,
    history_path: str | Path = RUN_HISTORY_PATH,
) -> dict:
    if limit <= 0:
        raise ValueError("history limit must be positive")
    rows = _history_rows(metrics_dir, history_path)
    if profile:
        rows = [row for row in rows if row.get("profile") == profile]
    selected = rows[-limit:]
    return {
        "runs": len(rows),
        "shown": len(selected),
        "profile": profile,
        "history": [concise_metrics(row) for row in selected],
    }


def compare_profiles(
    metrics_dir: str | Path = METRICS_DIR,
    history_path: str | Path = RUN_HISTORY_PATH,
) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in _history_rows(metrics_dir, history_path):
        groups[str(row.get("profile", "unknown"))].append(row)
    profiles = {}
    for profile, rows in sorted(groups.items()):
        elapsed = sum(float(row.get("elapsed_seconds", 0.0)) for row in rows)
        completed = sum(int(row.get("files_completed", 0)) for row in rows)
        failed = sum(int(row.get("files_failed", 0)) for row in rows)
        bytes_read = sum(float(row.get("bytes_read", 0)) for row in rows)
        profiles[profile] = {
            "runs": len(rows),
            "files_completed": completed,
            "files_failed": failed,
            "files_per_hour": round(completed / elapsed * 3600, 3) if elapsed else 0.0,
            "megabytes_per_second": round(bytes_read / 1_000_000 / elapsed, 3)
            if elapsed
            else 0.0,
            "peak_worker_rss_mb": max(
                (float((row.get("resources") or {}).get("peak_worker_rss_mb", 0.0)) for row in rows),
                default=0.0,
            ),
            "peak_vram_mb": max(
                (float((row.get("resources") or {}).get("peak_vram_mb", 0.0)) for row in rows),
                default=0.0,
            ),
        }
    return {"profiles": profiles}


def print_latest(metrics_dir: str | Path = METRICS_DIR, full: bool = False) -> None:
    payload = latest_metrics(metrics_dir)
    if payload is None:
        print("No extractor metrics have been recorded yet.")
        return
    print(json.dumps(payload if full else concise_metrics(payload), indent=2))
