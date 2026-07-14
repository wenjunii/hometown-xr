"""Stable filter contracts and reproducible run provenance."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from concepts import CONCEPT_ANCHORS
from config import (
    FILTER_SIGNATURE_SCHEMA_VERSION,
    LANG_DETECTION_THRESHOLD,
    MAX_PARAGRAPH_LENGTH,
    MIN_NARRATIVE_INDICATORS,
    MIN_PARAGRAPH_LENGTH,
    NARRATIVE_FILTER_ENABLED,
    NARRATIVE_RULESET_VERSION,
    PROJECT_ROOT,
    SEMANTIC_MODEL_NAME,
    SEMANTIC_MODEL_REVISION,
    SEMANTIC_THRESHOLD,
)
from keywords import get_all_keywords_flat


def filter_contract(
    semantic_threshold: float = SEMANTIC_THRESHOLD,
    language_threshold: float = LANG_DETECTION_THRESHOLD,
) -> dict:
    """Return the complete behavior contract that determines crawl output."""
    return {
        "schema_version": FILTER_SIGNATURE_SCHEMA_VERSION,
        "semantic_model": {
            "name": SEMANTIC_MODEL_NAME,
            "revision": SEMANTIC_MODEL_REVISION,
        },
        "semantic_threshold": round(float(semantic_threshold), 8),
        "language_threshold": round(float(language_threshold), 8),
        "paragraph_length": {
            "minimum": MIN_PARAGRAPH_LENGTH,
            "maximum": MAX_PARAGRAPH_LENGTH,
        },
        "narrative_filter": {
            "enabled": NARRATIVE_FILTER_ENABLED,
            "minimum_indicators": MIN_NARRATIVE_INDICATORS,
            "ruleset_version": NARRATIVE_RULESET_VERSION,
        },
        "keywords": sorted(get_all_keywords_flat()),
        "concept_anchors": list(CONCEPT_ANCHORS),
    }


def build_filter_signature(
    semantic_threshold: float = SEMANTIC_THRESHOLD,
    language_threshold: float = LANG_DETECTION_THRESHOLD,
) -> str:
    payload = json.dumps(
        filter_contract(semantic_threshold, language_threshold),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def current_git_commit(project_root: str | Path = PROJECT_ROOT) -> str:
    """Return the checked-out commit without failing non-Git installations."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def build_run_manifest(
    settings,
    crawl_ids: list[str],
    strategy: str,
    source_limit: int | None,
    chunk_size: int,
) -> dict:
    return {
        "schema_version": 1,
        "run_id": settings.run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": current_git_commit(),
        "filter_signature": settings.filter_signature,
        "filter_contract": filter_contract(
            settings.semantic_threshold,
            settings.language_threshold,
        ),
        "crawls": list(crawl_ids),
        "scheduling": {
            "strategy": strategy,
            "global_source_limit": source_limit,
            "chunk_size": chunk_size,
        },
        "runtime": {
            "profile": settings.profile_name,
            "workers": settings.workers,
            "candidate_batch_size": settings.candidate_batch_size,
            "inference_batch_size": settings.inference_batch_size,
            "encoding_batch_size": settings.encoding_batch_size,
            "precision": settings.precision,
            "adaptive_batching": settings.adaptive_batching,
            "cache_enabled": settings.cache_enabled,
        },
    }
