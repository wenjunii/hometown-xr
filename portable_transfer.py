"""Verified local snapshots and serial workstation handoffs, without a Git server."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from credential_guard import is_sensitive_path, scan_content
from run_lock import CrawlerRunLock
from workstation_guard import WorkstationGuardError, WorkstationLease, _atomic_json

CONFIG = "data/handoff.local.json"
OWNER = "data/.folder-owner.local.json"
MAX_FILES = 100_000
MAX_BYTES = 64 * 1024**3
MAX_MANIFEST_BYTES = 32 * 1024**2
logger = logging.getLogger(__name__)
_CODE_DIRS = {"scripts", "tests", "docs", "4090", "5090", ".github"}
_CODE_SUFFIXES = {".py", ".ps1", ".md", ".txt", ".toml", ".json", ".yml", ".yaml"}
_DATA_LOCAL = {"models", "cache", "metrics", "parquet", "evidence-local"}
_LOCAL_TREES = {"data/full_sources/_index_cache"}
_LOCAL_FILES = {
    CONFIG,
    OWNER,
    "data/.crawler.lock",
    "data/.workstation-owner.local.json",
    "data/.story-stop-request.json",
    "data/hardware-profile.local.json",
    "data/dependency-audit.local.json",
    "data/stories/_state/run-state.json",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path.name}")
    return value


def _safe_name(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("unsafe snapshot path")
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name:
        raise ValueError("non-canonical snapshot path")
    for part in path.parts:
        if part in {".", ".."} or part.rstrip(" .") != part:
            raise ValueError("unsafe snapshot path component")
        if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part):
            raise ValueError("reserved snapshot path component")
    return path


def _included(name: str) -> bool:
    parts = _safe_name(name).parts
    if any(
        name.casefold() == tree or name.casefold().startswith(tree + "/") for tree in _LOCAL_TREES
    ):
        return False
    if name in _LOCAL_FILES or is_sensitive_path(name):
        return False
    if any(p.startswith(".venv") or p == "__pycache__" for p in parts):
        return False
    if any(p.startswith(".") for p in parts[1:]):
        return False
    if name.endswith((".tmp", ".log", ".pyc", "-wal", "-shm", "-journal")):
        return False
    if parts[0] == "data":
        return (
            len(parts) > 1
            and parts[1] not in _DATA_LOCAL
            and not name.endswith(".local.json")
            and Path(name).suffix.lower()
            in {".db", ".json", ".jsonl", ".gz", ".md", ".txt", ".csv", ".tsv"}
        )
    if len(parts) == 1:
        return (
            name in {"LICENSE", ".gitignore", ".gitattributes"}
            or Path(name).suffix in _CODE_SUFFIXES
        )
    return parts[0] in _CODE_DIRS and Path(name).suffix in _CODE_SUFFIXES


def inventory(root: Path) -> dict[str, dict]:
    """Hash portable code and durable research data; never traverse local caches."""
    result = {}
    total_bytes = 0
    candidates = list(root.iterdir())
    while candidates:
        path = candidates.pop()
        relative = path.relative_to(root).as_posix()
        parts = PurePosixPath(relative).parts
        if relative.casefold() in _LOCAL_TREES:
            continue
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"links cannot enter a snapshot: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            if len(parts) == 1 and parts[0] not in _CODE_DIRS | {"data"}:
                continue
            if parts[0] == "data" and len(parts) == 2 and parts[1] in _DATA_LOCAL:
                continue
            if any(p.startswith(".") for p in parts[1:]) or "__pycache__" in parts:
                continue
            if is_sensitive_path(relative + "/file.txt"):
                continue
            candidates.extend(path.iterdir())
        elif stat.S_ISREG(metadata.st_mode) and _included(relative):
            result[relative] = {"size": metadata.st_size, "sha256": _hash(path)}
            total_bytes += result[relative]["size"]
            if relative == "data/progress.db":
                wal = Path(str(path) + "-wal")
                result[relative]["wal_sha256"] = _hash(wal) if wal.exists() else None
            if len(result) > MAX_FILES or total_bytes > MAX_BYTES:
                raise ValueError("snapshot exceeds transfer limits")
    return dict(sorted(result.items()))


def _scan(path: Path, name: str) -> None:
    """Scan decoded gzip text too; compressed files must not hide known secrets."""
    with path.open("rb") as handle:
        if handle.read(128).startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise ValueError(
                f"Git LFS pointer is not project data: {name}; hydrate it from an existing local copy first"
            )
    opener = gzip.open if path.suffix == ".gz" else open
    scan_name = name[:-3] if path.suffix == ".gz" else name
    tail = b""
    total = 0
    with opener(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024**2), b""):
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError(f"expanded file exceeds transfer limits: {name}")
            findings = scan_content(scan_name, tail + chunk)
            if findings:
                raise ValueError(f"credential scan blocked {name} [{findings[0].rule}]")
            tail = chunk[-8192:]


def _check_database(path: Path) -> None:
    logger.info("Checking database integrity and scanning stored text...")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("database integrity check failed")
        active = connection.execute(
            "SELECT COUNT(*) FROM processing_state WHERE status = 'processing'"
        ).fetchone()[0]
        if active:
            raise ValueError("database still contains active source leases; stop/recover first")
        # Read typed values rather than serializing every row back into SQL.
        # Binary values must not suppress assignment scanning in adjacent text.
        batch = bytearray()

        def scan_batch():
            if batch and scan_content("database.txt", bytes(batch)):
                raise ValueError("credential scan blocked database text")
            batch.clear()

        schemas = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'"
        ).fetchall()
        count = 0
        for table, definition in schemas:
            if definition:
                batch.extend(definition.encode() + b"\n")
            quoted = '"' + table.replace('"', '""') + '"'
            for row in connection.execute("SELECT * FROM " + quoted):
                for value in row:
                    if isinstance(value, bytes):
                        if scan_content("database.db", value):
                            raise ValueError("credential scan blocked database text")
                    elif isinstance(value, str):
                        batch.extend(value.replace("\0", "\n").encode() + b"\n")
                        if len(batch) >= 1024**2:
                            scan_batch()
                count += 1
                if count % 50_000 == 0:
                    logger.info("Scanned %s database records", count)
        scan_batch()
    finally:
        connection.close()


def _store_path(root: Path, store: str | Path | None) -> Path:
    if store is None:
        store = _json(root / CONFIG)["store"]
    path = Path(store).expanduser().resolve()
    if path == root or root in path.parents or path in root.parents:
        raise ValueError("snapshot store must be outside the project checkout")
    return path


@contextmanager
def _store_lock(store: Path):
    if not store.is_dir():
        raise ValueError("snapshot store is unavailable")
    lock = store / ".transfer-lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("snapshot store is busy; never remove a live transfer lock") from exc
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump({"host": socket.gethostname(), "pid": os.getpid(), "at": _now()}, handle)
        yield
    finally:
        lock.unlink()


def _control(store: Path) -> dict:
    control = _json(store / "control.json")
    if control.get("schema_version") != 1 or not re.fullmatch(
        r"[0-9a-f]{32}", control.get("store_id", "")
    ):
        raise ValueError("unsupported or invalid snapshot store")
    return control


def _config(root: Path, store: Path, control: dict, files: dict) -> None:
    _atomic_json(
        root / CONFIG,
        {
            "schema_version": 1,
            "mode": "folder",
            "store": str(store),
            "store_id": control["store_id"],
            "snapshot_id": control["snapshot_id"],
            "fingerprint": _digest(files),
            "code_digest": _digest({k: v for k, v in files.items() if not k.startswith("data/")}),
            "required_precision": control.get("required_precision"),
        },
    )


def _snapshot_path(store: Path, snapshot_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", snapshot_id):
        raise ValueError("invalid snapshot identifier")
    return store / "snapshots" / (snapshot_id + ".zip")


def _owned(root: Path, control: dict) -> bool:
    owner_path = root / OWNER
    local = _json(owner_path) if owner_path.exists() else {}
    owner = control.get("lease") or {}
    return bool(
        owner.get("owner_id")
        and owner.get("owner_id") == local.get("owner_id")
        and owner.get("host") == socket.gethostname()
    )


def _receiver_tool(store: Path, snapshot_id: str) -> Path:
    source = Path(__file__).resolve().parent
    target = store / "tools" / snapshot_id
    files = [
        "portable_transfer.py",
        "credential_guard.py",
        "run_lock.py",
        "config.py",
        "workstation_guard.py",
        "database_checkpoint.py",
        "deterministic_gzip.py",
        "story_packing.py",
        "story_products.py",
        "story_operations.py",
        "scripts/transfer.ps1",
    ]
    for name in files:
        _scan(source / name, name)
    for name in files:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
    return target / "scripts/transfer.ps1"


def _prepare_stories(staging: Path) -> None:
    from story_packing import _pack_payloads, build_story_packs, verify_story_packs
    from story_products import build_story_catalog

    stories = staging / "data/stories"
    catalog = stories / "_packs/catalog.json.gz"
    if catalog.exists():
        if not verify_story_packs(stories)["valid"]:
            raise ValueError("existing story packs failed integrity checks")
        payloads, _ = _pack_payloads(stories)
        for name, content in payloads.items():
            path = stories / name
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
    if any((stories / "_records").glob("*.jsonl.gz")):
        if not build_story_catalog(stories)["valid"]:
            raise ValueError("story fragments failed integrity checks")
        build_story_packs(stories)


def export_snapshot(root: Path, store: str | Path | None = None, *, apply: bool = False) -> dict:
    root = root.resolve()
    target = _store_path(root, store)
    files = inventory(root)
    if "data/progress.db" not in files:
        raise ValueError("restore/initialize the progress database before exporting")
    if (root / "data/.workstation-owner.local.json").exists():
        raise ValueError("release the existing Git workstation lease before switching transport")
    result = {
        "action": "export",
        "applied": False,
        "files": len(files),
        "bytes": sum(row["size"] for row in files.values()),
        "store": str(target),
    }
    if not apply:
        return result
    target.mkdir(parents=True, exist_ok=True)
    with CrawlerRunLock("local-transfer", path=root / "data/.crawler.lock"), _store_lock(target):
        control_path = target / "control.json"
        control = (
            _control(target)
            if control_path.exists()
            else {
                "schema_version": 1,
                "store_id": uuid.uuid4().hex,
                "revision": 0,
                "snapshot_id": None,
                "lease": None,
            }
        )
        local_config = _json(root / CONFIG) if (root / CONFIG).exists() else None
        if control["snapshot_id"] is not None:
            if not local_config or local_config.get("store_id") != control["store_id"]:
                raise ValueError("receive this store's latest snapshot before publishing")
            if local_config.get("snapshot_id") != control["snapshot_id"]:
                raise ValueError("stale workstation snapshot; refusing to replace newer work")
        if (control.get("lease") or {}).get("state") == "active" and not _owned(root, control):
            raise ValueError("snapshot store is owned by another workstation")
        files = inventory(root)
        logger.info(
            "Exporting %s portable files; original project files stay unchanged", len(files)
        )
        _check_database(root / "data/progress.db")
        from config import HARDWARE_PROFILES

        precisions = {profile.precision for profile in HARDWARE_PROFILES.values()}
        precision = next(iter(precisions)) if len(precisions) == 1 else None
        override_path = root / "data/hardware-profile.local.json"
        if override_path.exists():
            override = _json(override_path)
            if override.get("profile") in HARDWARE_PROFILES and override.get("precision") in {
                "fp16",
                "fp32",
            }:
                precision = override["precision"]
        required = (local_config or {}).get("required_precision")
        if required and precision != required:
            raise ValueError(
                "precision differs from the snapshot contract; validate the change separately"
            )
        snapshot_id = uuid.uuid4().hex
        archive_path = _snapshot_path(target, snapshot_id)
        archive_path.parent.mkdir(exist_ok=True)
        temporary = archive_path.with_suffix(".zip.tmp")
        try:
            with tempfile.TemporaryDirectory(prefix="hometown-export-") as directory:
                staging = Path(directory)
                for name in files:
                    destination = staging / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(root / name, destination)
                # SQLite's backup API creates a consistent, standalone database, including WAL pages.
                source = sqlite3.connect(
                    (root / "data/progress.db").as_uri() + "?mode=ro", uri=True
                )
                database = sqlite3.connect(staging / "data/progress.db")
                try:
                    source.backup(database)
                finally:
                    database.close()
                    source.close()
                from database_checkpoint import archive_database

                archive_database(
                    staging / "data/progress.db", staging / "data/checkpoints/progress.db.gz"
                )
                _prepare_stories(staging)
                from checkpoint import verify_output_integrity

                if not verify_output_integrity(staging / "data/output")["valid"]:
                    raise ValueError("accepted output failed integrity checks")
                packaged = inventory(staging)
                for name in packaged:
                    _scan(staging / name, name)
                if files != inventory(root):
                    raise ValueError("project changed during export; stop all writers and retry")
                manifest = {
                    "schema_version": 1,
                    "snapshot_id": snapshot_id,
                    "store_id": control["store_id"],
                    "parent_id": control["snapshot_id"],
                    "created_at": _now(),
                    "files": packaged,
                    "required_precision": precision,
                    "excludes_credentials_and_machine_state": True,
                }
                with zipfile.ZipFile(
                    temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
                ) as archive:
                    archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
                    for name in packaged:
                        archive.write(staging / name, "payload/" + name)
            verify_snapshot(temporary)
            os.replace(temporary, archive_path)
            receiver = _receiver_tool(target, snapshot_id)
            control.update(
                snapshot_id=snapshot_id,
                revision=control["revision"] + 1,
                lease=None,
                required_precision=precision,
            )
            _atomic_json(control_path, control)
            _config(root, target, control, files)
            (root / OWNER).unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        **result,
        "applied": True,
        "files": len(packaged),
        "bytes": sum(row["size"] for row in packaged.values()),
        "snapshot_id": snapshot_id,
        "path": str(archive_path),
        "archive_bytes": archive_path.stat().st_size,
        "sha256": _hash(archive_path),
        "receiver_script": str(receiver),
    }


def _manifest(archive: zipfile.ZipFile) -> dict:
    members = archive.infolist()
    if len(members) > MAX_FILES + 1:
        raise ValueError("snapshot has too many entries")
    names = [entry.filename for entry in members]
    if len(set(name.casefold() for name in names)) != len(names):
        raise ValueError("duplicate or case-colliding snapshot entries")
    if (
        "manifest.json" not in names
        or archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES
    ):
        raise ValueError("missing or oversized snapshot manifest")
    manifest = json.loads(archive.read("manifest.json"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("files"), dict)
    ):
        raise ValueError("unsupported snapshot manifest")
    for key in ("snapshot_id", "store_id"):
        if not re.fullmatch(r"[0-9a-f]{32}", manifest.get(key, "")):
            raise ValueError("invalid snapshot identity")
    files = manifest["files"]
    if manifest.get("required_precision") not in {None, "fp16", "fp32"}:
        raise ValueError("invalid snapshot precision contract")
    if "data/progress.db" not in files:
        raise ValueError("snapshot is missing its database")
    if set(names) != {"manifest.json", *("payload/" + name for name in files)}:
        raise ValueError("snapshot entries do not match the manifest")
    total = 0
    for name, row in files.items():
        if not _included(name):
            raise ValueError(f"forbidden snapshot path: {name}")
        entry = archive.getinfo("payload/" + name)
        if stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG):
            raise ValueError("snapshot contains a link or special file")
        if not isinstance(row, dict) or type(row.get("size")) is not int or row["size"] < 0:
            raise ValueError("invalid snapshot file size")
        if entry.file_size != row["size"] or not re.fullmatch(
            r"[0-9a-f]{64}", row.get("sha256", "")
        ):
            raise ValueError("invalid snapshot file metadata")
        total += row["size"]
    if total > MAX_BYTES:
        raise ValueError("snapshot exceeds transfer limits")
    return manifest


def _extract(archive_path: Path, target: Path) -> dict:
    logger.info("Verifying and staging snapshot %s", archive_path.name)
    with zipfile.ZipFile(archive_path) as archive:
        manifest = _manifest(archive)
        for name, row in manifest["files"].items():
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            total = 0
            with archive.open("payload/" + name) as source, destination.open("xb") as output:
                for chunk in iter(lambda: source.read(1024**2), b""):
                    total += len(chunk)
                    if total > row["size"]:
                        raise ValueError("snapshot file exceeds declared size")
                    digest.update(chunk)
                    output.write(chunk)
            if total != row["size"] or digest.hexdigest() != row["sha256"]:
                raise ValueError(f"snapshot checksum mismatch: {name}")
            _scan(destination, name)
        _check_database(target / "data/progress.db")
        from database_checkpoint import database_sync_status

        if not database_sync_status(
            target / "data/progress.db", target / "data/checkpoints/progress.db.gz"
        )["synchronized"]:
            raise ValueError("database and compressed checkpoint do not match")
        if (target / "data/stories/_packs/catalog.json.gz").exists():
            from story_packing import story_pack_status
            from story_products import verify_story_integrity

            if not story_pack_status(target / "data/stories")["safe_to_pull"]:
                raise ValueError("story packs and fragments do not match")
            if not verify_story_integrity(target / "data/stories")["valid"]:
                raise ValueError("story catalog failed integrity checks")
    return manifest


def verify_snapshot(path: str | Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="hometown-verify-") as directory:
        manifest = _extract(Path(path), Path(directory))
    return {
        "valid": True,
        "snapshot_id": manifest["snapshot_id"],
        "store_id": manifest["store_id"],
        "required_precision": manifest.get("required_precision"),
        "files": len(manifest["files"]),
        "sha256": _hash(Path(path)),
    }


def import_snapshot(
    root: Path, store: str | Path, destination: str | Path, *, apply: bool = False
) -> dict:
    root = root.resolve()
    source = Path(store).expanduser().resolve()
    target = Path(destination).resolve()
    if target.exists():
        raise ValueError(
            "destination already exists; use a new directory to preserve all local work"
        )
    if target == source or source in target.parents or target in source.parents:
        raise ValueError("destination must be separate from the snapshot store")
    with _store_lock(source):
        control = _control(source)
        archive_path = _snapshot_path(source, control["snapshot_id"] or "")
        if (control.get("lease") or {}).get("state") == "active":
            raise ValueError("finish/export the owning workstation's work before receiving")
        if apply and not target.parent.is_dir():
            raise ValueError("destination parent must already exist")
        # Only a newly created sibling is ever removed on failure. Existing projects are never replaced.
        staging = Path(
            tempfile.mkdtemp(prefix=".hometown-import-", dir=target.parent if apply else None)
        )
        try:
            manifest = _extract(archive_path, staging)
            if (
                manifest["store_id"] != control["store_id"]
                or manifest["snapshot_id"] != control["snapshot_id"]
            ):
                raise ValueError("snapshot/store identity mismatch")
            if manifest.get("required_precision") != control.get("required_precision"):
                raise ValueError("snapshot/store precision mismatch")
            result = {
                "action": "import",
                "applied": apply,
                "valid": True,
                "destination": str(target),
                "snapshot_id": manifest["snapshot_id"],
                "store_id": manifest["store_id"],
                "files": len(manifest["files"]),
                "required_precision": manifest.get("required_precision"),
                "sha256": _hash(archive_path),
            }
            if apply:
                _config(staging, source, control, inventory(staging))
                if target.exists():
                    raise ValueError("destination appeared during restore; refusing to overwrite")
                staging.rename(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return result


class FolderWorkstationLease(WorkstationLease):
    """Reuse ownership bookkeeping, replacing Git CAS with an exclusive store lock."""

    def __init__(self, profile: str, *, root: Path, **kwargs):
        super().__init__(profile, root=root, state_path=root / OWNER, **kwargs)
        self.store = _store_path(root, None)

    def _read_remote(self):
        control = _control(self.store)
        if control["store_id"] != _json(self.root / CONFIG)["store_id"]:
            raise WorkstationGuardError("snapshot store identity changed")
        return str(control["revision"]), control.get("lease")

    def _publish(self, payload: dict, parent: str | None) -> str:
        with _store_lock(self.store):
            control = _control(self.store)
            if str(control["revision"]) != parent:
                raise WorkstationGuardError("another workstation changed ownership")
            control.update(revision=control["revision"] + 1, lease=payload)
            _atomic_json(self.store / "control.json", control)
        return str(control["revision"])

    def preflight(self, *, allow_uncheckpointed_state=False):
        config = _json(self.root / CONFIG)
        control = _control(self.store)
        if (
            config["store_id"] != control["store_id"]
            or config["snapshot_id"] != control["snapshot_id"]
        ):
            raise WorkstationGuardError(
                "stale local snapshot; receive the latest into a new directory"
            )
        files = inventory(self.root)
        code = {k: v for k, v in files.items() if not k.startswith("data/")}
        if _digest(code) != config["code_digest"]:
            raise WorkstationGuardError(
                "code changed since handoff; export a new snapshot before running"
            )
        from config import HARDWARE_PROFILES

        if self.profile in HARDWARE_PROFILES and config.get("required_precision"):
            precision = HARDWARE_PROFILES[self.profile].precision
            override_path = self.root / "data/hardware-profile.local.json"
            if override_path.exists():
                override = _json(override_path)
                if override.get("profile") == self.profile and override.get("precision") in {
                    "fp16",
                    "fp32",
                }:
                    precision = override["precision"]
            if precision != config["required_precision"]:
                raise WorkstationGuardError(
                    f"snapshot requires {config['required_precision']}; set and validate this PC's local precision before running"
                )
        if not allow_uncheckpointed_state and _digest(files) != config["fingerprint"]:
            raise WorkstationGuardError("local research data has uncheckpointed changes")
        return {
            "ready": True,
            "transport": "folder",
            "head": config["code_digest"],
            "branch": "local-snapshot",
            "snapshot_id": config["snapshot_id"],
        }

    def claim(self, *, force_recovery=False):
        _, existing = self._read_remote()
        if force_recovery:
            raise WorkstationGuardError(
                "folder ownership is never force-stolen; recover the owning PC"
            )
        if existing and existing.get("state") == "active":
            if not self._same_owner(existing, self._local_state(), self.host):
                raise WorkstationGuardError(
                    f"snapshot is owned by {existing.get('host')}; ownership does not auto-expire"
                )
        return super().claim()

    def status(self, *, include_preflight=False):
        result = super().status(include_preflight=include_preflight)
        result.update(
            transport="folder",
            store=str(self.store),
            remote_ref=None,
            active=bool(result["lease"] and result["lease"].get("state") == "active"),
        )
        return result


def store_status(root: Path, store: str | Path | None = None) -> dict:
    # A shipped receiver lives inside the store; inspection is read-only.
    path = (
        Path(store).expanduser().resolve()
        if store is not None
        else _store_path(root.resolve(), None)
    )
    control = _control(path)
    return {
        "transport": "folder",
        "store": str(path),
        **control,
        "snapshots": len(list((path / "snapshots").glob("*.zip"))),
    }


def check_local_files(root: Path) -> dict:
    files = inventory(root.resolve())
    for name in files:
        _scan(root / name, name)
    if "data/progress.db" in files:
        _check_database(root / "data/progress.db")
    return {
        "valid": True,
        "scanned_files": len(files),
        "transport": "local",
        "credentials_found": False,
    }


def register_parser(subparsers):
    parser = subparsers.add_parser("transfer", help=__doc__)
    commands = parser.add_subparsers(dest="transfer_command", required=True)
    for action in ("export", "import", "status"):
        command = commands.add_parser(action)
        command.add_argument("--store", type=Path, required=action == "import")
        if action != "status":
            command.add_argument("--yes", action="store_true")
        if action == "import":
            command.add_argument("--destination", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--path", type=Path, required=True)
    commands.add_parser("check", help="scan portable files locally without Git or a network")


def execute(args, root: Path) -> dict:
    if args.transfer_command == "export":
        return export_snapshot(root, args.store, apply=args.yes)
    if args.transfer_command == "import":
        return import_snapshot(root, args.store, args.destination, apply=args.yes)
    if args.transfer_command == "verify":
        return verify_snapshot(args.path)
    if args.transfer_command == "check":
        return check_local_files(root)
    return store_status(root, args.store)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    register_parser(parser.add_subparsers(dest="command", required=True))
    try:
        result = execute(parser.parse_args(), Path(__file__).resolve().parent)
    except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile, RuntimeError, KeyError) as exc:
        parser.exit(1, f"Transfer stopped: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
