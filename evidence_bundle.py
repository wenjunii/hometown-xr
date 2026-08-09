"""Credential-free, portable model and real-workload evidence bundles."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from config import (
    DATA_DIR,
    EVIDENCE_OUTBOX_DIR,
    HARDWARE_PROFILES,
    LOCAL_EVIDENCE_DIR,
    PROFILE_EVIDENCE_DIR,
)
from signatures import build_filter_signature, current_git_commit

SCHEMA_VERSION = 1
_PROHIBITED_KEYS = {
    "annotation_path",
    "audit_root",
    "host",
    "hostname",
    "password",
    "secret",
    "token",
    "credential",
}


def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _safe_model_snapshot(payload: dict) -> dict:
    samples = [
        {
            "sample_id": str(row.get("sample_id", "")),
            "semantic_score": row.get("semantic_score"),
            "concept_match": row.get("concept_match"),
            "above_threshold": bool(row.get("above_threshold")),
        }
        for row in payload.get("samples", [])
    ]
    return {
        "schema_version": payload.get("schema_version"),
        "created_at": payload.get("created_at"),
        "git_commit": payload.get("git_commit"),
        "profile": payload.get("profile"),
        "model": payload.get("model") or {},
        "libraries": payload.get("libraries") or {},
        "samples": samples,
        "sample_count": len(samples),
        "sample_digest": payload.get("sample_digest"),
    }


def _validate_model_snapshot(payload: dict, profile: str) -> None:
    if payload.get("profile") != profile:
        raise ValueError("model snapshot profile does not match the bundle profile")
    samples = payload.get("samples") or []
    digest = hashlib.sha256(
        json.dumps(samples, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if payload.get("sample_count") != len(samples) or payload.get("sample_digest") != digest:
        raise ValueError("model snapshot sample digest is invalid")


def _walk_keys(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).lower()
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def portable_evidence_path(profile: str) -> Path:
    if profile not in HARDWARE_PROFILES:
        raise ValueError(f"unknown hardware profile: {profile}")
    return PROFILE_EVIDENCE_DIR / f"{profile}.json"


def load_profile_evidence(profile: str) -> dict | None:
    path = portable_evidence_path(profile)
    if not path.exists():
        return None
    return validate_evidence_bundle(path)


def validate_evidence_bundle(path: str | Path) -> dict:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported profile evidence schema")
    profile = str(payload.get("profile", ""))
    if profile not in HARDWARE_PROFILES:
        raise ValueError("profile evidence names an unsupported GPU profile")
    body = {key: value for key, value in payload.items() if key != "content_sha256"}
    if payload.get("content_sha256") != _sha256(body):
        raise ValueError("profile evidence content digest is invalid")
    prohibited = sorted(set(_walk_keys(body)) & _PROHIBITED_KEYS)
    if prohibited:
        raise ValueError("profile evidence contains prohibited keys: " + ", ".join(prohibited))
    snapshot = payload.get("model_snapshot")
    if snapshot is not None:
        _validate_model_snapshot(snapshot, profile)
    workload = payload.get("workload_benchmark")
    if workload is not None:
        if workload.get("profile") != profile:
            raise ValueError("workload benchmark profile does not match the bundle")
        if workload.get("mode") != "isolated_real_sources":
            raise ValueError("workload evidence is not an isolated real-source benchmark")
        if not workload.get("trial_outputs_agree"):
            raise ValueError("workload benchmark trials do not have equivalent output")
    return payload


def export_evidence_bundle(
    profile: str,
    output_path: str | Path | None = None,
    *,
    candidate_path: str | Path | None = None,
    workload_path: str | Path | None = None,
) -> dict:
    if profile not in HARDWARE_PROFILES:
        raise ValueError(f"unknown hardware profile: {profile}")
    candidate = Path(candidate_path or DATA_DIR / "evaluation" / f"model-candidate-{profile}.json")
    workload = Path(workload_path or LOCAL_EVIDENCE_DIR / f"workload-{profile}.json")
    snapshot = None
    benchmark = None
    if candidate.exists():
        snapshot = _safe_model_snapshot(json.loads(candidate.read_text(encoding="utf-8")))
        _validate_model_snapshot(snapshot, profile)
    if workload.exists():
        raw = json.loads(workload.read_text(encoding="utf-8"))
        benchmark = {
            key: raw.get(key)
            for key in (
                "schema_version",
                "generated_at",
                "git_commit",
                "filter_signature",
                "mode",
                "profile",
                "crawl_id",
                "sources_per_trial",
                "historical_state_changed",
                "cache_enabled",
                "trials",
                "trial_outputs_agree",
                "recommended_workers",
                "warning",
            )
        }
    if snapshot is None and benchmark is None:
        raise ValueError("no local model snapshot or workload benchmark is available")
    body = {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "git_commit": current_git_commit(),
        "filter_signature": build_filter_signature(),
        "component_created_at": {
            "model_snapshot": (snapshot or {}).get("created_at"),
            "workload_benchmark": (benchmark or {}).get("generated_at"),
        },
        "generated_text": False,
        "contains_source_text": False,
        "contains_credentials": False,
        "model_snapshot": snapshot,
        "workload_benchmark": benchmark,
    }
    payload = {**body, "content_sha256": _sha256(body)}
    target = Path(output_path or EVIDENCE_OUTBOX_DIR / f"profile-evidence-{profile}.json")
    _atomic_json(target, payload)
    return {
        "schema_version": 1,
        "profile": profile,
        "path": str(target.resolve()),
        "content_sha256": payload["content_sha256"],
        "model_snapshot": snapshot is not None,
        "workload_benchmark": benchmark is not None,
        "portable": True,
        "credentials_included": False,
    }


def import_evidence_bundle(path: str | Path, *, apply: bool = False) -> dict:
    payload = validate_evidence_bundle(path)
    target = portable_evidence_path(str(payload["profile"]))
    result = {
        "schema_version": 1,
        "profile": payload["profile"],
        "source": str(Path(path).resolve()),
        "target": str(target),
        "content_sha256": payload["content_sha256"],
        "valid": True,
        "dry_run": not apply,
        "applied": False,
    }
    if apply:
        if target.exists():
            current = validate_evidence_bundle(target)
            if current.get("content_sha256") == payload["content_sha256"]:
                result["unchanged"] = True
                result["applied"] = True
                return result
        _atomic_json(target, payload)
        result["applied"] = True
    return result


def portable_evidence_status() -> dict:
    profiles = {}
    for profile in sorted(HARDWARE_PROFILES):
        path = portable_evidence_path(profile)
        if not path.exists():
            profiles[profile] = {"present": False, "path": str(path)}
            continue
        try:
            payload = validate_evidence_bundle(path)
            profiles[profile] = {
                "present": True,
                "valid": True,
                "path": str(path),
                "content_sha256": payload["content_sha256"],
                "model_snapshot": payload.get("model_snapshot") is not None,
                "workload_benchmark": payload.get("workload_benchmark") is not None,
            }
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            profiles[profile] = {
                "present": True,
                "valid": False,
                "path": str(path),
                "error": str(exc),
            }
    return {
        "schema_version": 1,
        "profiles": profiles,
        "complete_model_profiles": [
            name for name, row in profiles.items() if row.get("valid") and row.get("model_snapshot")
        ],
        "complete_workload_profiles": [
            name
            for name, row in profiles.items()
            if row.get("valid") and row.get("workload_benchmark")
        ],
    }


def portable_profile_metrics() -> dict[str, dict]:
    result = {}
    for profile in HARDWARE_PROFILES:
        payload = load_profile_evidence(profile)
        workload = (payload or {}).get("workload_benchmark")
        if not workload:
            continue
        trials = workload.get("trials") or []
        result[profile] = {
            "runs": len(trials),
            "files_completed": sum(int(row.get("completed", 0)) for row in trials),
            "files_failed": sum(int(row.get("failed", 0)) for row in trials),
            "files_per_hour": max(
                (float(row.get("files_per_hour") or 0.0) for row in trials),
                default=0.0,
            ),
            "peak_worker_rss_mb": max(
                (float(row.get("peak_worker_rss_mb") or 0.0) for row in trials),
                default=0.0,
            ),
            "peak_vram_mb": max(
                (float(row.get("peak_vram_mb") or 0.0) for row in trials),
                default=0.0,
            ),
            "portable_evidence": True,
        }
    return result
