"""Lightweight operational metrics for long extractor runs."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import METRICS_DIR, METRICS_FLUSH_SECONDS


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
    ):
        self.metrics_dir = Path(metrics_dir)
        self.session_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
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
            "keyword_candidates": 0,
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
        }

    def add_target_files(self, count: int) -> None:
        self.target_files += max(0, count)
        self.flush()

    def record_inference(
        self,
        candidates: int,
        matches: int,
        seconds: float,
        cache_stats: dict[str, int] | None = None,
        runtime_stats: dict[str, int] | None = None,
    ) -> None:
        self.counters["keyword_candidates"] += candidates
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
        # Matches become durable only when their source transaction commits.
        del matches
        self.flush()

    def record_source(
        self,
        status: str,
        records: int,
        candidates: int,
        matches: int,
        bytes_read: int,
        parse_seconds: float,
    ) -> None:
        key = f"files_{status}"
        if key in self.counters:
            self.counters[key] += 1
        self.counters["records_processed"] += records
        self.counters["matches_committed"] += matches
        self.counters["bytes_read"] += bytes_read
        self.counters["parse_seconds"] += parse_seconds
        # Candidates are counted by inference batches, including batches that
        # span multiple sources, so they are intentionally not added here.
        del candidates
        self.flush()

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
        return {
            "schema_version": 2,
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
        }

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


def latest_metrics(metrics_dir: str | Path = METRICS_DIR) -> dict | None:
    path = Path(metrics_dir) / "latest.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def print_latest(metrics_dir: str | Path = METRICS_DIR) -> None:
    payload = latest_metrics(metrics_dir)
    if payload is None:
        print("No extractor metrics have been recorded yet.")
        return
    print(json.dumps(payload, indent=2))
