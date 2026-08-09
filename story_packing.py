"""Deterministic packed transport for resumable per-source story fragments."""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import io
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from config import STORIES_DIR
from deterministic_gzip import gzip_binary_writer

STORY_PACK_SCHEMA_VERSION = 1
STORY_PACK_COUNT = 64
_FRAGMENT_PATH = re.compile(r"^_records/[0-9a-f]{20}\.jsonl\.gz$")
_PACK_PATH = re.compile(r"^pack-[0-9a-f]{2}\.jsonl\.gz$")


def story_pack_dir(stories_dir: str | Path = STORIES_DIR) -> Path:
    return Path(stories_dir) / "_packs"


def story_pack_catalog_path(stories_dir: str | Path = STORIES_DIR) -> Path:
    return story_pack_dir(stories_dir) / "catalog.json.gz"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_gzip_lines(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as raw:
        with gzip_binary_writer(raw) as compressed:
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


def _atomic_gzip_json(path: Path, payload: dict) -> None:
    _atomic_gzip_lines(path, [payload])


def _read_gzip_rows(path: Path) -> list[dict]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Story pack is unreadable: {path}") from exc


def _pack_name(fragment_name: str) -> str:
    bucket = int(fragment_name[:2], 16) % STORY_PACK_COUNT
    return f"pack-{bucket:02x}.jsonl.gz"


def _fragment_metadata(path: str, payload: bytes) -> dict:
    return {
        "path": path,
        "sha256": _sha256_bytes(payload),
        "bytes": len(payload),
    }


def _fragment_fingerprint(rows: list[dict]) -> str:
    normalized = [
        {
            "path": str(row["path"]),
            "sha256": str(row["sha256"]),
            "bytes": int(row["bytes"]),
        }
        for row in sorted(rows, key=lambda row: str(row["path"]))
    ]
    payload = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _pack_payloads(stories_dir: str | Path) -> tuple[dict[str, bytes], list[str]]:
    root = Path(stories_dir)
    payloads: dict[str, bytes] = {}
    errors = []
    for pack_path in sorted(story_pack_dir(root).glob("pack-*.jsonl.gz")):
        if not _PACK_PATH.fullmatch(pack_path.name):
            errors.append(f"unexpected pack filename: {pack_path.name}")
            continue
        try:
            rows = _read_gzip_rows(pack_path)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        for index, row in enumerate(rows, 1):
            relative = str(row.get("path", ""))
            if not _FRAGMENT_PATH.fullmatch(relative):
                errors.append(
                    f"{pack_path.name}:{index}: invalid fragment path {relative!r}"
                )
                continue
            if relative in payloads:
                errors.append(f"duplicate packed fragment: {relative}")
                continue
            try:
                payload = base64.b64decode(
                    str(row.get("payload_base64", "")),
                    validate=True,
                )
            except (binascii.Error, ValueError):
                errors.append(
                    f"{pack_path.name}:{index}: invalid base64 payload"
                )
                continue
            try:
                expected_bytes = int(row.get("bytes", -1))
            except (TypeError, ValueError):
                errors.append(
                    f"{pack_path.name}:{index}: invalid fragment byte count"
                )
                continue
            expected_sha = str(row.get("sha256", ""))
            if len(payload) != expected_bytes:
                errors.append(
                    f"{pack_path.name}:{index}: fragment byte count mismatch"
                )
                continue
            if _sha256_bytes(payload) != expected_sha:
                errors.append(
                    f"{pack_path.name}:{index}: fragment checksum mismatch"
                )
                continue
            payloads[relative] = payload
    return payloads, errors


def build_story_packs(stories_dir: str | Path = STORIES_DIR) -> dict:
    """Pack local source fragments into a bounded deterministic transfer set."""
    root = Path(stories_dir)
    fragments = sorted((root / "_records").glob("*.jsonl.gz"))
    groups: dict[str, list[dict]] = defaultdict(list)
    fragment_rows = []
    for fragment in fragments:
        relative = fragment.relative_to(root).as_posix()
        if not _FRAGMENT_PATH.fullmatch(relative):
            raise RuntimeError(f"Unsafe story fragment path: {relative}")
        payload = fragment.read_bytes()
        metadata = _fragment_metadata(relative, payload)
        fragment_rows.append(metadata)
        groups[_pack_name(fragment.name)].append(
            {
                "schema_version": STORY_PACK_SCHEMA_VERSION,
                **metadata,
                "payload_base64": base64.b64encode(payload).decode("ascii"),
            }
        )

    pack_root = story_pack_dir(root)
    expected_names = set(groups)
    for stale in pack_root.glob("pack-*.jsonl.gz"):
        if stale.name not in expected_names:
            stale.unlink()
    pack_entries = []
    for name in sorted(groups):
        path = pack_root / name
        _atomic_gzip_lines(
            path,
            sorted(groups[name], key=lambda row: str(row["path"])),
        )
        pack_entries.append(
            {
                "path": name,
                "sha256": _sha256_path(path),
                "bytes": path.stat().st_size,
                "fragments": len(groups[name]),
            }
        )
    catalog = {
        "schema_version": STORY_PACK_SCHEMA_VERSION,
        "pack_count": len(pack_entries),
        "pack_limit": STORY_PACK_COUNT,
        "fragments": len(fragment_rows),
        "fragment_bytes": sum(int(row["bytes"]) for row in fragment_rows),
        "fragment_fingerprint": _fragment_fingerprint(fragment_rows),
        "packs": pack_entries,
    }
    _atomic_gzip_json(story_pack_catalog_path(root), catalog)
    return {
        "valid": True,
        "catalog_path": str(story_pack_catalog_path(root)),
        **{key: value for key, value in catalog.items() if key != "packs"},
        "pack_bytes": sum(int(row["bytes"]) for row in pack_entries),
    }


def verify_story_packs(stories_dir: str | Path = STORIES_DIR) -> dict:
    """Verify pack catalog checksums and every embedded source fragment."""
    root = Path(stories_dir)
    catalog_path = story_pack_catalog_path(root)
    errors = []
    if not catalog_path.exists():
        return {
            "valid": False,
            "catalog_exists": False,
            "catalog_path": str(catalog_path),
            "errors": ["story pack catalog is missing"],
        }
    try:
        rows = _read_gzip_rows(catalog_path)
        catalog = rows[0] if len(rows) == 1 else None
    except RuntimeError as exc:
        catalog = None
        errors.append(str(exc))
    if not isinstance(catalog, dict):
        errors.append("story pack catalog must contain exactly one object")
        return {
            "valid": False,
            "catalog_exists": True,
            "catalog_path": str(catalog_path),
            "errors": errors,
        }
    try:
        catalog_schema = int(catalog.get("schema_version", -1))
    except (TypeError, ValueError):
        catalog_schema = -1
    if catalog_schema != STORY_PACK_SCHEMA_VERSION:
        errors.append("story pack catalog schema is unsupported")
    pack_rows = catalog.get("packs", [])
    if not isinstance(pack_rows, list):
        pack_rows = []
        errors.append("story pack catalog packs must be a list")
    expected = {}
    for row in pack_rows:
        if not isinstance(row, dict):
            errors.append("story pack catalog contains a non-object pack entry")
            continue
        name = str(row.get("path", ""))
        if not _PACK_PATH.fullmatch(name):
            errors.append(f"story pack catalog contains an invalid path: {name!r}")
            continue
        if name in expected:
            errors.append(f"story pack catalog contains a duplicate pack: {name}")
            continue
        expected[name] = row
    try:
        catalog_pack_count = int(catalog.get("pack_count", -1))
    except (TypeError, ValueError):
        catalog_pack_count = -1
    if catalog_pack_count != len(pack_rows):
        errors.append("story pack catalog pack count is invalid")
    actual = {
        path.name: path
        for path in story_pack_dir(root).glob("pack-*.jsonl.gz")
    }
    missing_packs = sorted(set(expected) - set(actual))
    uncovered_packs = sorted(set(actual) - set(expected))
    for name, row in expected.items():
        path = actual.get(name)
        if path is None:
            continue
        try:
            expected_bytes = int(row.get("bytes", -1))
        except (TypeError, ValueError):
            expected_bytes = -1
            errors.append(f"{name}: invalid catalog byte count")
        if path.stat().st_size != expected_bytes:
            errors.append(f"{name}: pack byte count mismatch")
        if _sha256_path(path) != str(row.get("sha256", "")):
            errors.append(f"{name}: pack checksum mismatch")
    payloads, payload_errors = _pack_payloads(root)
    errors.extend(payload_errors)
    fragment_rows = [
        _fragment_metadata(path, payload)
        for path, payload in sorted(payloads.items())
    ]
    fingerprint = _fragment_fingerprint(fragment_rows)
    try:
        catalog_fragments = int(catalog.get("fragments", -1))
        catalog_fragment_bytes = int(catalog.get("fragment_bytes", -1))
    except (TypeError, ValueError):
        catalog_fragments = -1
        catalog_fragment_bytes = -1
        errors.append("story pack catalog fragment totals are invalid")
    if len(payloads) != catalog_fragments:
        errors.append("packed fragment count does not match the catalog")
    if sum(len(payload) for payload in payloads.values()) != catalog_fragment_bytes:
        errors.append("packed fragment bytes do not match the catalog")
    if fingerprint != str(catalog.get("fragment_fingerprint", "")):
        errors.append("packed fragment fingerprint does not match the catalog")
    errors.extend(f"missing pack: {name}" for name in missing_packs)
    errors.extend(f"uncovered pack: {name}" for name in uncovered_packs)
    return {
        "valid": not errors,
        "catalog_exists": True,
        "catalog_path": str(catalog_path),
        "packs": len(actual),
        "fragments": len(payloads),
        "fragment_bytes": sum(len(payload) for payload in payloads.values()),
        "fragment_fingerprint": fingerprint,
        "missing_packs": missing_packs,
        "uncovered_packs": uncovered_packs,
        "errors": errors,
    }


def story_pack_status(stories_dir: str | Path = STORIES_DIR) -> dict:
    """Compare ignored local fragments with the synchronized pack payloads."""
    root = Path(stories_dir)
    verification = verify_story_packs(root)
    if not verification["valid"]:
        return {
            "valid": False,
            "safe_to_pull": False,
            "packs": verification,
            "matching_fragments": 0,
            "missing_fragments": 0,
            "changed_fragments": [],
            "extra_fragments": [],
        }
    payloads, _errors = _pack_payloads(root)
    local = {
        path.relative_to(root).as_posix(): path
        for path in (root / "_records").glob("*.jsonl.gz")
    }
    changed = sorted(
        relative
        for relative in set(payloads) & set(local)
        if _sha256_path(local[relative]) != _sha256_bytes(payloads[relative])
    )
    matching = sum(
        _sha256_path(local[relative]) == _sha256_bytes(payloads[relative])
        for relative in set(payloads) & set(local)
    )
    return {
        "valid": True,
        "safe_to_pull": not changed and not (set(local) - set(payloads)),
        "packs": verification,
        "matching_fragments": matching,
        "missing_fragments": len(set(payloads) - set(local)),
        "changed_fragments": changed,
        "extra_fragments": sorted(set(local) - set(payloads)),
    }


def restore_story_packs(
    stories_dir: str | Path = STORIES_DIR,
    *,
    replace: bool = False,
) -> dict:
    """Restore verified fragment bytes without deleting uncheckpointed extras."""
    root = Path(stories_dir)
    verification = verify_story_packs(root)
    if not verification["valid"]:
        raise RuntimeError("Story packs failed verification and cannot be restored")
    payloads, _errors = _pack_payloads(root)
    restored = 0
    replaced = 0
    skipped = 0
    conflicts = []
    for relative, payload in sorted(payloads.items()):
        target = root / relative
        if target.exists():
            if _sha256_path(target) == _sha256_bytes(payload):
                skipped += 1
                continue
            if not replace:
                conflicts.append(relative)
                continue
            replaced += 1
        else:
            restored += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".restore.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, target)
    if conflicts:
        raise RuntimeError(
            f"{len(conflicts)} local story fragment(s) differ from the checkpoint"
        )
    local_paths = {
        path.relative_to(root).as_posix()
        for path in (root / "_records").glob("*.jsonl.gz")
    }
    return {
        "valid": True,
        "restored_fragments": restored,
        "replaced_fragments": replaced,
        "skipped_fragments": skipped,
        "extra_local_fragments": sorted(local_paths - set(payloads)),
        "fragments": len(payloads),
    }
