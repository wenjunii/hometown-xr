"""Cross-PC ownership and checkpoint-freshness protection.

The shared lease is a small JSON commit stored in a dedicated Git notes ref.
It never enters the project branch and contains no credentials. Normal
fast-forward push semantics provide the compare-and-swap needed to keep two
workstations from claiming the same checkpoint concurrently.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    PROJECT_ROOT,
    WORKSTATION_LEASE_HOURS,
    WORKSTATION_LEASE_RENEW_MINUTES,
    WORKSTATION_OWNER_PATH,
)
from database_checkpoint import database_sync_status

REMOTE_LEASE_REF = "refs/notes/hometown-xr-workstation-owner"
LOCAL_REMOTE_LEASE_REF = "refs/remotes/origin/hometown-xr-workstation-owner"
logger = logging.getLogger(__name__)


class WorkstationGuardError(RuntimeError):
    """Raised when workstation ownership or freshness cannot be proven."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def lease_is_active(payload: dict | None, now: datetime | None = None) -> bool:
    """Return whether a lease payload represents unexpired active ownership."""
    if not payload or payload.get("state") != "active":
        return False
    expires_at = _parse_time(payload.get("expires_at"))
    return bool(expires_at and expires_at > (now or _utc_now()))


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class WorkstationLease:
    """Acquire, renew, and release one repository-wide workstation lease."""

    def __init__(
        self,
        profile: str,
        *,
        root: str | Path = PROJECT_ROOT,
        state_path: str | Path = WORKSTATION_OWNER_PATH,
        lease_hours: int = WORKSTATION_LEASE_HOURS,
        renew_minutes: int = WORKSTATION_LEASE_RENEW_MINUTES,
        host: str | None = None,
        now=_utc_now,
    ):
        if lease_hours <= 0 or renew_minutes <= 0:
            raise ValueError("lease duration and renewal interval must be positive")
        self.profile = profile
        self.root = Path(root)
        self.state_path = Path(state_path)
        self.lease_hours = lease_hours
        self.renew_minutes = renew_minutes
        self.host = host or socket.gethostname()
        self.now = now
        self.owner_id: str | None = None
        self._last_renewed: datetime | None = None

    def __enter__(self) -> "WorkstationLease":
        self.claim()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self.finalize()
        except Exception:
            if exc_type is None:
                raise
            logger.exception("Unable to finalize workstation ownership after failure")

    def _git(
        self,
        *arguments: str,
        input_text: str | None = None,
        check: bool = True,
    ) -> str:
        environment = os.environ.copy()
        environment.setdefault("GIT_AUTHOR_NAME", "Hometown XR")
        environment.setdefault("GIT_AUTHOR_EMAIL", "hometown-xr@local.invalid")
        environment.setdefault("GIT_COMMITTER_NAME", environment["GIT_AUTHOR_NAME"])
        environment.setdefault("GIT_COMMITTER_EMAIL", environment["GIT_AUTHOR_EMAIL"])
        process = subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        if check and process.returncode:
            detail = process.stderr.strip() or process.stdout.strip()
            raise WorkstationGuardError(
                f"git {' '.join(arguments)} failed: {detail or process.returncode}"
            )
        return process.stdout.strip()

    def preflight(self, *, allow_uncheckpointed_state: bool = False) -> dict:
        """Prove that branch, checkpoint, and story packs match shared state."""
        self._git("fetch", "--quiet", "--prune", "origin")
        branch = self._git("branch", "--show-current")
        if not branch:
            raise WorkstationGuardError("detached HEAD cannot own a crawl checkpoint")
        head = self._git("rev-parse", "HEAD")
        try:
            upstream = self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
            upstream_head = self._git("rev-parse", "@{upstream}")
        except WorkstationGuardError as exc:
            raise WorkstationGuardError(
                f"branch {branch} has no synchronized upstream; push or pull it first"
            ) from exc
        dirty_paths = [
            line[3:] for line in self._git("status", "--porcelain").splitlines() if line.strip()
        ]
        database = database_sync_status()
        story_packs = {"valid": True, "safe_to_pull": True}
        try:
            from story_packing import story_pack_catalog_path, story_pack_status

            if story_pack_catalog_path().exists():
                story_packs = story_pack_status()
        except (OSError, ValueError) as exc:
            story_packs = {
                "valid": False,
                "safe_to_pull": False,
                "error": str(exc),
            }
        errors = []
        if head != upstream_head:
            errors.append(f"HEAD does not match {upstream}")
        if dirty_paths:
            errors.append("worktree has uncheckpointed tracked changes")
        if not allow_uncheckpointed_state and not database.get("synchronized"):
            errors.append("working database does not match the shared archive")
        if not story_packs.get("valid"):
            errors.append("local story packs or fragments are invalid")
        elif not allow_uncheckpointed_state and not story_packs.get("safe_to_pull", True):
            errors.append("local story fragments do not match the shared packs")
        result = {
            "schema_version": 1,
            "ready": not errors,
            "host": self.host,
            "profile": self.profile,
            "branch": branch,
            "head": head,
            "upstream": upstream,
            "upstream_head": upstream_head,
            "dirty_paths": dirty_paths,
            "database": database,
            "story_packs": story_packs,
            "allows_uncheckpointed_state": allow_uncheckpointed_state,
            "errors": errors,
        }
        if errors:
            raise WorkstationGuardError("; ".join(errors))
        return result

    def _remote_commit(self) -> str | None:
        listing = self._git("ls-remote", "--refs", "origin", REMOTE_LEASE_REF)
        if not listing:
            return None
        commit = listing.split()[0]
        self._git(
            "fetch",
            "--quiet",
            "origin",
            f"+{REMOTE_LEASE_REF}:{LOCAL_REMOTE_LEASE_REF}",
        )
        return commit

    def _read_remote(self) -> tuple[str | None, dict | None]:
        commit = self._remote_commit()
        if commit is None:
            return None, None
        message = self._git("show", "-s", "--format=%B", commit)
        try:
            payload = json.loads(message)
        except json.JSONDecodeError as exc:
            raise WorkstationGuardError("shared workstation lease is unreadable") from exc
        return commit, payload

    def _publish(self, payload: dict, parent: str | None) -> str:
        tree = self._git("rev-parse", "HEAD^{tree}")
        arguments = ["commit-tree", tree]
        if parent:
            arguments.extend(["-p", parent])
        commit = self._git(
            *arguments,
            input_text=json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        )
        try:
            self._git("push", "--quiet", "origin", f"{commit}:{REMOTE_LEASE_REF}")
        except WorkstationGuardError as exc:
            raise WorkstationGuardError(
                "another workstation changed ownership; refresh status before retrying"
            ) from exc
        confirmed = self._remote_commit()
        if confirmed != commit:
            raise WorkstationGuardError("remote workstation ownership could not be verified")
        return commit

    def _local_state(self) -> dict | None:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkstationGuardError("local workstation ownership is unreadable") from exc

    def status(self, *, include_preflight: bool = False) -> dict:
        commit, payload = self._read_remote()
        result = {
            "schema_version": 1,
            "remote_ref": REMOTE_LEASE_REF,
            "remote_commit": commit,
            "active": lease_is_active(payload, self.now()),
            "lease": payload,
            "local": self._local_state(),
        }
        if include_preflight:
            try:
                result["preflight"] = self.preflight()
            except WorkstationGuardError as exc:
                result["preflight"] = {"ready": False, "errors": [str(exc)]}
        return result

    @staticmethod
    def _same_owner(existing: dict | None, local: dict | None, host: str) -> bool:
        return bool(
            existing
            and local
            and existing.get("owner_id")
            and existing.get("owner_id") == local.get("owner_id")
            and existing.get("host") == host
        )

    def claim(self, *, force_recovery: bool = False) -> dict:
        parent, existing = self._read_remote()
        local = self._local_state() or {}
        now = self.now()
        same_owner = self._same_owner(existing, local, self.host)
        if lease_is_active(existing, now):
            if not same_owner:
                raise WorkstationGuardError(
                    "checkpoint is owned by "
                    f"{existing.get('host', 'another workstation')} "
                    f"({existing.get('profile', 'unknown')}) until "
                    f"{existing.get('expires_at', 'unknown')}"
                )
        elif (
            existing
            and existing.get("checkpoint_state") == "uncheckpointed"
            and not same_owner
            and not force_recovery
        ):
            raise WorkstationGuardError(
                "expired ownership contains uncheckpointed work from "
                f"{existing.get('host', 'another workstation')}; recover that PC or "
                "use explicit force recovery only if its local work is permanently lost"
            )
        preflight = self.preflight(allow_uncheckpointed_state=same_owner)
        recovered_from = None
        if force_recovery and existing and not same_owner:
            recovered_from = {
                "owner_id": existing.get("owner_id"),
                "host": existing.get("host"),
                "profile": existing.get("profile"),
                "lease_commit": parent,
            }
        if same_owner:
            owner_id = str(existing["owner_id"])
            acquired_at = str(existing.get("acquired_at") or now.isoformat())
        else:
            owner_id = uuid.uuid4().hex
            acquired_at = now.isoformat()
        payload = {
            "schema_version": 1,
            "state": "active",
            "owner_id": owner_id,
            "host": self.host,
            "profile": self.profile,
            "project_commit": preflight["head"],
            "project_branch": preflight["branch"],
            "acquired_at": acquired_at,
            "renewed_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=self.lease_hours)).isoformat(),
            "checkpoint_state": (
                existing.get("checkpoint_state", "synchronized") if same_owner else "synchronized"
            ),
        }
        if recovered_from:
            payload["force_recovered_from"] = recovered_from
        payload["lease_commit"] = self._publish(payload, parent)
        _atomic_json(self.state_path, payload)
        self.owner_id = owner_id
        self._last_renewed = now
        return payload

    def retain(self) -> dict:
        """Keep ownership active until durable state is checkpointed and pushed."""
        local = self._local_state()
        if not local:
            return {"retained": False, "reason": "not_owned"}
        parent, existing = self._read_remote()
        if not self._same_owner(existing, local, self.host):
            raise WorkstationGuardError("workstation ownership was lost")
        now = self.now()
        payload = {
            **existing,
            "state": "active",
            "checkpoint_state": "uncheckpointed",
            "renewed_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=self.lease_hours)).isoformat(),
        }
        payload.pop("lease_commit", None)
        payload["lease_commit"] = self._publish(payload, parent)
        _atomic_json(self.state_path, payload)
        self.owner_id = str(payload["owner_id"])
        self._last_renewed = now
        return {"retained": True, "lease": payload}

    def finalize(self) -> dict:
        """Release synchronized no-op work, otherwise retain local ownership."""
        try:
            self.preflight()
        except WorkstationGuardError:
            return self.retain()
        return self.release()

    def renew_if_due(self, *, force: bool = False) -> dict | None:
        now = self.now()
        if not force and self._last_renewed is not None:
            if now - self._last_renewed < timedelta(minutes=self.renew_minutes):
                return None
        local = self._local_state()
        if not local:
            raise WorkstationGuardError("this workstation has no active ownership token")
        parent, existing = self._read_remote()
        if not existing or existing.get("owner_id") != local.get("owner_id"):
            raise WorkstationGuardError("workstation ownership was lost")
        payload = {
            **existing,
            "state": "active",
            "renewed_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=self.lease_hours)).isoformat(),
        }
        payload.pop("lease_commit", None)
        payload["lease_commit"] = self._publish(payload, parent)
        _atomic_json(self.state_path, payload)
        self.owner_id = str(payload["owner_id"])
        self._last_renewed = now
        return payload

    def release(self) -> dict:
        local = self._local_state()
        if not local:
            return {"released": False, "reason": "not_owned"}
        self.preflight()
        parent, existing = self._read_remote()
        if not self._same_owner(existing, local, self.host):
            raise WorkstationGuardError("refusing to release ownership held elsewhere")
        now = self.now()
        payload = {
            **existing,
            "state": "released",
            "released_at": now.isoformat(),
            "expires_at": now.isoformat(),
            "checkpoint_state": "synchronized",
        }
        payload.pop("lease_commit", None)
        payload["lease_commit"] = self._publish(payload, parent)
        self.state_path.unlink(missing_ok=True)
        self.owner_id = None
        self._last_renewed = None
        return {"released": True, "lease": payload}
