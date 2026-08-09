"""A small cross-platform lock that prevents two local crawler sessions."""

from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import RUN_LOCK_PATH


class CrawlerAlreadyRunning(RuntimeError):
    pass


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_run_lock(path: str | Path = RUN_LOCK_PATH) -> dict | None:
    """Return the current lock payload, or None when no lock exists."""
    lock_path = Path(path)
    try:
        return json.loads(lock_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"path": str(lock_path), "state": "unreadable"}


class CrawlerRunLock:
    def __init__(self, profile: str, path: str | Path = RUN_LOCK_PATH):
        self.path = Path(path)
        self.profile = profile
        self._owned = False

    def __enter__(self) -> "CrawlerRunLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "profile": self.profile,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

        for _attempt in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                existing = read_run_lock(self.path) or {}
                same_host = existing.get("host") == socket.gethostname()
                pid = int(existing.get("pid", 0) or 0)
                if same_host and not pid_is_running(pid):
                    self.path.unlink(missing_ok=True)
                    continue
                details = json.dumps(existing, sort_keys=True)
                raise CrawlerAlreadyRunning(
                    f"Crawler lock already exists at {self.path}: {details}"
                )
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True)
                self._owned = True
                return

        raise CrawlerAlreadyRunning(f"Could not acquire crawler lock {self.path}")

    def release(self) -> None:
        if self._owned:
            self.path.unlink(missing_ok=True)
            self._owned = False
