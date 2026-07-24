"""Cross-process graceful shutdown control for story enrichment."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from config import RUN_LOCK_PATH, STORY_STOP_REQUEST_PATH
from run_lock import pid_is_running, read_run_lock

logger = logging.getLogger("story_control")


def _request_target_pid(payload: dict | None) -> int:
    if not payload:
        return 0
    try:
        return int(payload.get("target_pid", payload.get("pid", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def _request_targets_current_run(payload: dict | None, run_token: str | None) -> bool:
    target_pid = _request_target_pid(payload)
    if target_pid == os.getpid():
        return True
    request_token = str((payload or {}).get("run_token", ""))
    return bool(run_token and request_token == run_token)


def _read_request(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None


def request_story_shutdown(
    lock_path: str | Path = RUN_LOCK_PATH,
    request_path: str | Path = STORY_STOP_REQUEST_PATH,
) -> dict:
    """Ask the active local story-enrichment process to stop gracefully."""
    active = read_run_lock(lock_path)
    if not active:
        return {"requested": False, "reason": "no active crawler run"}
    if active.get("profile") != "story-enrichment":
        return {
            "requested": False,
            "reason": f"active run is {active.get('profile', 'unknown')}, not story enrichment",
        }
    if active.get("host") != socket.gethostname():
        return {
            "requested": False,
            "reason": "the story-enrichment lock belongs to another computer",
        }

    try:
        target_pid = int(active.get("pid", 0) or 0)
    except (TypeError, ValueError):
        target_pid = 0
    if not pid_is_running(target_pid):
        return {
            "requested": False,
            "reason": "the story-enrichment lock is stale",
            "pid": target_pid,
        }

    target = Path(request_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "target_pid": target_pid,
        "host": socket.gethostname(),
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)
    return {
        "requested": True,
        "pid": target_pid,
        "request_path": str(target),
    }


def _watch_for_story_shutdown(
    shutdown_event: threading.Event,
    watcher_stop: threading.Event,
    request_path: Path,
    poll_seconds: float,
    run_token: str | None,
) -> None:
    while not watcher_stop.is_set():
        if _request_targets_current_run(_read_request(request_path), run_token):
            if not shutdown_event.is_set():
                logger.info(
                    "External shutdown request received. "
                    "Returning active sources to the checkpoint safely..."
                )
            shutdown_event.set()
            return
        watcher_stop.wait(poll_seconds)


@contextmanager
def watch_story_shutdown(
    shutdown_event: threading.Event,
    request_path: str | Path = STORY_STOP_REQUEST_PATH,
    poll_seconds: float = 0.25,
    run_token: str | None = None,
) -> Iterator[None]:
    """Set an event when another process targets this story-enrichment PID."""
    target = Path(request_path)
    effective_run_token = run_token or os.environ.get("HOMETOWN_XR_STORY_RUN_TOKEN")
    watcher_stop = threading.Event()
    watcher = threading.Thread(
        target=_watch_for_story_shutdown,
        args=(
            shutdown_event,
            watcher_stop,
            target,
            poll_seconds,
            effective_run_token,
        ),
        name="story-shutdown-watcher",
        daemon=True,
    )
    watcher.start()
    try:
        yield
    finally:
        watcher_stop.set()
        watcher.join(timeout=max(1.0, poll_seconds * 2))
        request = _read_request(target)
        if _request_targets_current_run(request, effective_run_token):
            target.unlink(missing_ok=True)
