"""Durable operational state for deterministic story enrichment."""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    STORIES_DIR,
    STORY_AUTO_MAX_WORKERS,
    STORY_AUTO_MIN_WORKERS,
    STORY_AUTO_SUCCESS_WINDOW,
    STORY_MAX_SOURCE_ATTEMPTS,
    STORY_RETRY_BASE_SECONDS,
    STORY_RETRY_MAX_SECONDS,
)
from run_lock import pid_is_running

STORY_OPERATIONS_SCHEMA_VERSION = 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def story_state_dir(stories_dir: str | Path = STORIES_DIR) -> Path:
    return Path(stories_dir) / "_state"


def story_run_state_path(stories_dir: str | Path = STORIES_DIR) -> Path:
    return story_state_dir(stories_dir) / "run-state.json"


def story_failure_ledger_path(stories_dir: str | Path = STORIES_DIR) -> Path:
    return story_state_dir(stories_dir) / "failures.jsonl.gz"


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_gzip_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as handle:
                for row in rows:
                    handle.write(
                        json.dumps(
                            row,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
    os.replace(temporary, path)


def _read_gzip_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Story failure ledger is unreadable: {path}") from exc


def categorize_story_failure(result: dict) -> str:
    """Return a stable operational category for one unsuccessful source."""
    error = str(result.get("error", "")).lower()
    if match := re.search(
        r"(?:http|status|response|server error|client error)[^0-9]{0,20}([45]\d{2})"
        r"|([45]\d{2})[^a-z]{0,10}(?:error|response)",
        error,
    ):
        return f"http_{match.group(1) or match.group(2)}"
    if "429" in error:
        return "http_429"
    if "503" in error:
        return "http_503"
    if "timeout" in error or "timed out" in error:
        return "timeout"
    if any(
        marker in error
        for marker in (
            "connection",
            "name resolution",
            "remote end closed",
            "network",
        )
    ):
        return "connection"
    if result.get("missing_record_ids"):
        return "missing_records"
    return "other"


class StoryFailureLedger:
    """Persist source attempts, cooldowns, and quarantine state."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_attempts: int = STORY_MAX_SOURCE_ATTEMPTS,
        retry_base_seconds: int = STORY_RETRY_BASE_SECONDS,
        retry_max_seconds: int = STORY_RETRY_MAX_SECONDS,
    ):
        self.path = Path(path)
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self._rows = {
            str(row["source_file"]): row
            for row in _read_gzip_rows(self.path)
            if row.get("source_file")
        }
        self._lock = threading.Lock()

    def rows(self) -> list[dict]:
        return [dict(self._rows[key]) for key in sorted(self._rows)]

    def state(self, source_file: str, now: datetime | None = None) -> str:
        row = self._rows.get(source_file)
        if row is None:
            return "ready"
        if int(row.get("attempts", 0)) >= self.max_attempts:
            return "quarantined"
        retry_at = row.get("next_retry_at")
        if retry_at:
            try:
                if datetime.fromisoformat(str(retry_at)) > (now or _utc_now()):
                    return "cooldown"
            except ValueError:
                pass
        return "ready"

    def summary(self, now: datetime | None = None) -> dict:
        counts = {"ready": 0, "cooldown": 0, "quarantined": 0}
        categories: dict[str, int] = {}
        for source_file, row in self._rows.items():
            counts[self.state(source_file, now)] += 1
            category = str(row.get("category", "other"))
            categories[category] = categories.get(category, 0) + 1
        return {
            "sources": len(self._rows),
            **{f"{key}_sources": value for key, value in counts.items()},
            "categories": dict(sorted(categories.items())),
        }

    def record_result(self, result: dict, now: datetime | None = None) -> None:
        source_file = str(result["source_file"])
        status = str(result.get("status", "failed"))
        if status == "interrupted":
            return
        with self._lock:
            if status == "completed":
                if self._rows.pop(source_file, None) is not None:
                    self._write()
                return
            if status not in {"failed", "partial"}:
                return
            moment = now or _utc_now()
            previous = self._rows.get(source_file, {})
            attempts = int(previous.get("attempts", 0)) + 1
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** max(0, attempts - 1)),
            )
            self._rows[source_file] = {
                "schema_version": STORY_OPERATIONS_SCHEMA_VERSION,
                "source_file": source_file,
                "crawl_id": str(result.get("crawl_id", "")),
                "status": status,
                "category": categorize_story_failure(result),
                "attempts": attempts,
                "first_failed_at": str(previous.get("first_failed_at") or _iso(moment)),
                "last_failed_at": _iso(moment),
                "next_retry_at": (
                    None
                    if attempts >= self.max_attempts
                    else _iso(moment + timedelta(seconds=delay))
                ),
                "missing_matches": len(result.get("missing_record_ids", [])),
                "last_error": result.get("error"),
            }
            self._write()

    def reset(
        self,
        *,
        source_files: set[str] | None = None,
        crawl_ids: set[str] | None = None,
        reset_all: bool = False,
    ) -> list[dict]:
        selected = []
        with self._lock:
            for source_file, row in list(self._rows.items()):
                if not (
                    reset_all
                    or (source_files and source_file in source_files)
                    or (crawl_ids and str(row.get("crawl_id", "")) in crawl_ids)
                ):
                    continue
                selected.append(self._rows.pop(source_file))
            if selected:
                self._write()
        return sorted(selected, key=lambda row: str(row["source_file"]))

    def _write(self) -> None:
        _write_gzip_rows(self.path, self.rows())


class AdaptiveWorkerController:
    """Conservatively adjust network concurrency from source outcomes."""

    pressure_categories = {
        "connection",
        "http_429",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "timeout",
    }

    def __init__(
        self,
        workers: int | str,
        selected_sources: int,
        *,
        auto_min: int = STORY_AUTO_MIN_WORKERS,
        auto_max: int = STORY_AUTO_MAX_WORKERS,
        success_window: int = STORY_AUTO_SUCCESS_WINDOW,
    ):
        self.auto = str(workers).lower() == "auto"
        if selected_sources <= 0:
            self.maximum = 0
            self.minimum = 0
            self.target = 0
            self.success_window = max(1, success_window)
            self.success_streak = 0
            self.adjustments = []
            return
        requested = auto_min if self.auto else int(workers)
        maximum = auto_max if self.auto else requested
        self.maximum = max(1, min(maximum, max(1, selected_sources)))
        self.minimum = max(1, min(auto_min if self.auto else requested, self.maximum))
        self.target = max(self.minimum, min(requested, self.maximum))
        self.success_window = max(1, success_window)
        self.success_streak = 0
        self.adjustments: list[dict] = []

    @property
    def mode(self) -> str:
        return "auto" if self.auto else "fixed"

    def observe(self, result: dict) -> bool:
        if not self.auto:
            return False
        previous = self.target
        category = categorize_story_failure(result)
        if result.get("error") and category in self.pressure_categories:
            self.success_streak = 0
            self.target = max(self.minimum, max(1, self.target // 2))
            reason = category
        elif not result.get("error") and result.get("status") in {"completed", "partial"}:
            self.success_streak += 1
            reason = "successful_sources"
            if self.success_streak >= self.success_window and self.target < self.maximum:
                self.target += 1
                self.success_streak = 0
        else:
            self.success_streak = 0
            reason = category
        if self.target == previous:
            return False
        self.adjustments.append(
            {
                "from": previous,
                "to": self.target,
                "reason": reason,
                "at": _iso(_utc_now()),
            }
        )
        return True


class StoryRunTelemetry:
    """Atomically expose one enrichment run to read-only status commands."""

    def __init__(
        self,
        path: str | Path,
        *,
        worker_mode: str,
        configured_workers: int | str,
        target_workers: int,
        maximum_workers: int,
        selected_sources: int,
    ):
        self.path = Path(path)
        now = _utc_now()
        self.started = now
        self.active: dict[str, dict] = {}
        self._completed_records = 0
        self._completed_eligible_paragraphs = 0
        self._lock = threading.RLock()
        self.payload = {
            "schema_version": STORY_OPERATIONS_SCHEMA_VERSION,
            "run_id": uuid.uuid4().hex,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "status": "running",
            "started_at": _iso(now),
            "updated_at": _iso(now),
            "worker_mode": worker_mode,
            "configured_workers": configured_workers,
            "target_workers": target_workers,
            "maximum_workers": maximum_workers,
            "active_workers": 0,
            "selected_sources": selected_sources,
            "finished_sources": 0,
            "remaining_run_sources": selected_sources,
            "unresolved_sources": 0,
            "completed_sources": 0,
            "partial_sources": 0,
            "failed_sources": 0,
            "interrupted_sources": 0,
            "stories_written": 0,
            "records_processed": 0,
            "eligible_paragraphs": 0,
            "sources_per_hour": 0.0,
            "eta_seconds": None,
            "active_sources": [],
            "worker_adjustments": [],
        }
        self._write()

    def source_started(self, source_file: str, crawl_id: str) -> None:
        with self._lock:
            self.active[source_file] = {
                "source_file": source_file,
                "crawl_id": crawl_id,
                "started_at": _iso(_utc_now()),
                "records_processed": 0,
                "eligible_paragraphs": 0,
            }
            self._refresh()

    def source_progress(
        self,
        source_file: str,
        records_processed: int,
        eligible_paragraphs: int,
    ) -> None:
        with self._lock:
            active = self.active.get(source_file)
            if active is None:
                return
            active["records_processed"] = records_processed
            active["eligible_paragraphs"] = eligible_paragraphs
            self._refresh()

    def source_finished(
        self,
        result: dict,
        *,
        target_workers: int,
        adjustments: list[dict],
    ) -> None:
        with self._lock:
            self.active.pop(str(result["source_file"]), None)
            status = str(result.get("status", "failed"))
            status_key = f"{status}_sources"
            if status_key in self.payload:
                self.payload[status_key] += 1
            if status != "completed":
                self.payload["unresolved_sources"] += 1
            self.payload["finished_sources"] += 1
            self.payload["remaining_run_sources"] = max(
                0,
                self.payload["selected_sources"] - self.payload["finished_sources"],
            )
            self.payload["stories_written"] += int(result.get("stories", 0))
            self._completed_records += int(result.get("records_processed", 0))
            self._completed_eligible_paragraphs += int(
                result.get("eligible_paragraphs", 0)
            )
            self.payload["target_workers"] = target_workers
            self.payload["worker_adjustments"] = adjustments
            self._refresh()

    def finish(self, status: str) -> None:
        with self._lock:
            self.payload["status"] = status
            self.active.clear()
            self._refresh()

    def _refresh(self) -> None:
        now = _utc_now()
        elapsed = max(0.001, (now - self.started).total_seconds())
        finished = int(self.payload["finished_sources"])
        rate = finished / elapsed * 3600
        remaining = int(self.payload["remaining_run_sources"])
        self.payload["updated_at"] = _iso(now)
        self.payload["active_workers"] = len(self.active)
        self.payload["active_sources"] = [
            self.active[key] for key in sorted(self.active)
        ]
        self.payload["records_processed"] = self._completed_records + sum(
            int(row.get("records_processed", 0)) for row in self.active.values()
        )
        self.payload[
            "eligible_paragraphs"
        ] = self._completed_eligible_paragraphs + sum(
            int(row.get("eligible_paragraphs", 0)) for row in self.active.values()
        )
        self.payload["sources_per_hour"] = round(rate, 3)
        self.payload["eta_seconds"] = (
            round(remaining / rate * 3600)
            if rate > 0 and remaining > 0
            else 0
            if remaining == 0
            else None
        )
        self._write()

    def _write(self) -> None:
        _write_json_atomic(self.path, self.payload)


def read_story_run_state(stories_dir: str | Path = STORIES_DIR) -> dict | None:
    path = story_run_state_path(stories_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    same_host = payload.get("host") == socket.gethostname()
    running = same_host and pid_is_running(int(payload.get("pid", 0) or 0))
    payload["process_running"] = running
    if payload.get("status") == "running" and not running:
        payload["status"] = "stale"
    now = _utc_now()
    for source in payload.get("active_sources", []):
        try:
            started = datetime.fromisoformat(str(source["started_at"]))
            source["elapsed_seconds"] = round(
                max(0.0, (now - started).total_seconds())
            )
        except (KeyError, TypeError, ValueError):
            source["elapsed_seconds"] = None
    return payload


def story_failure_status(stories_dir: str | Path = STORIES_DIR) -> dict:
    ledger = StoryFailureLedger(story_failure_ledger_path(stories_dir))
    return {
        "schema_version": STORY_OPERATIONS_SCHEMA_VERSION,
        **ledger.summary(),
        "failures": ledger.rows(),
    }


def reset_story_failures(
    stories_dir: str | Path = STORIES_DIR,
    *,
    source_files: set[str] | None = None,
    crawl_ids: set[str] | None = None,
    reset_all: bool = False,
    apply: bool = False,
) -> dict:
    ledger = StoryFailureLedger(story_failure_ledger_path(stories_dir))
    selected = [
        row
        for row in ledger.rows()
        if (
            reset_all
            or (source_files and str(row["source_file"]) in source_files)
            or (crawl_ids and str(row.get("crawl_id", "")) in crawl_ids)
        )
    ]
    reset_rows = (
        ledger.reset(
            source_files=source_files,
            crawl_ids=crawl_ids,
            reset_all=reset_all,
        )
        if apply
        else []
    )
    return {
        "schema_version": STORY_OPERATIONS_SCHEMA_VERSION,
        "selected_sources": len(selected),
        "selection": selected,
        "applied": apply,
        "reset_sources": len(reset_rows),
        "remaining": ledger.summary(),
    }
