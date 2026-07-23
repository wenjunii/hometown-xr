"""Central configuration for the Hometown XR Common Crawl extractor."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = DATA_DIR / "output"
MODELS_DIR = DATA_DIR / "models"
DB_PATH = DATA_DIR / "progress.db"
DB_ARCHIVE_PATH = DATA_DIR / "checkpoints" / "progress.db.gz"
RUN_LOCK_PATH = DATA_DIR / ".crawler.lock"
METRICS_DIR = DATA_DIR / "metrics"
EVALUATION_DIR = DATA_DIR / "evaluation"
MODEL_BASELINE_PATH = EVALUATION_DIR / "model-baseline.json"
AUDIT_DIR = DATA_DIR / "audits"
AUDIT_EVIDENCE_DIR = DATA_DIR / "checkpoints" / "audit-evidence"
REPLAY_PATH = EVALUATION_DIR / "replay.jsonl.gz"
RUN_HISTORY_PATH = DATA_DIR / "run-history.jsonl.gz"
PARQUET_DIR = DATA_DIR / "parquet"
STORIES_DIR = DATA_DIR / "stories"
CACHE_DIR = DATA_DIR / "cache"
INFERENCE_CACHE_PATH = CACHE_DIR / "inference.db"
HARDWARE_OVERRIDE_PATH = DATA_DIR / "hardware-profile.local.json"

for directory in (DATA_DIR, OUTPUT_DIR, MODELS_DIR, STORIES_DIR):
    directory.mkdir(parents=True, exist_ok=True)


# Common Crawl
CC_BASE_URL = "https://data.commoncrawl.org/"
DEFAULT_CRAWL_ID = "CC-MAIN-2026-12"


# Matching
SEMANTIC_THRESHOLD = 0.45
MIN_PARAGRAPH_LENGTH = 150
MAX_PARAGRAPH_LENGTH = 5000
NARRATIVE_FILTER_ENABLED = True
MIN_NARRATIVE_INDICATORS = 8
SEMANTIC_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
SEMANTIC_MODEL_REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"


# Language detection
FASTTEXT_MODEL_URL = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.bin"
FASTTEXT_MODEL_PATH = MODELS_DIR / "lid.176.bin"
LANG_DETECTION_THRESHOLD = 0.5
LANG_DETECT_CHARS = 500


# Network
HTTP_TIMEOUT = 60
HTTP_RETRIES = 5
HTTP_BACKOFF_FACTOR = 1.0
HTTP_BACKOFF_JITTER = 1.0
HTTP_RECOVERY_SUCCESSES = 4
HTTP_CIRCUIT_BASE_SECONDS = 30
HTTP_CIRCUIT_MAX_SECONDS = 600


# Retry and lease behavior
MAX_FILE_ATTEMPTS = 4
RETRY_BASE_SECONDS = 300
RETRY_MAX_SECONDS = 21_600
RETRY_JITTER_FRACTION = 0.25
LEASE_TIMEOUT_SECONDS = 600
HEARTBEAT_INTERVAL_SECONDS = 30
PROCESS_POOL_MAX_RESTARTS = 3
PROCESS_POOL_RECYCLE_SOURCES = 500
WORKER_RSS_RECYCLE_MB = 1536
METRICS_FLUSH_SECONDS = 10
EVALUATION_SAMPLE_RATE = 0.002
EVALUATION_UNCERTAIN_SAMPLE_RATE = 0.25
EVALUATION_MINORITY_PROBE_RATE = 0.02
EVALUATION_MINORITY_LANGUAGE_QUOTA = 20
EVALUATION_MAX_SAMPLES_PER_SESSION = 2_000
EVALUATION_MIN_BASELINE_LABELS = 100
EVALUATION_MIN_LANGUAGE_LABELS = 20
EVALUATION_MIN_HOLDOUT_LABELS = 20
EVALUATION_HOLDOUT_RATE = 0.20
EVALUATION_SHADOW_SAMPLES_PER_SOURCE = 2
EVALUATION_SHADOW_SOURCE_RATE = EVALUATION_SAMPLE_RATE
EVALUATION_REPLAY_MAX_SAMPLES = 20_000
AUDIT_DEFAULT_PER_CRAWL = 2
AUDIT_MAX_PER_CRAWL = 10
AUDIT_MIN_ADOPTION_SOURCES = 5
AUDIT_SAMPLE_RATE = 0.05
OUTPUT_SCHEMA_VERSION = 5
SUPPORTED_OUTPUT_SCHEMA_VERSIONS = frozenset({2, 3, 4, OUTPUT_SCHEMA_VERSION})
FILTER_SIGNATURE_SCHEMA_VERSION = 1
NARRATIVE_RULESET_VERSION = "2026-07-16.1"
TEXT_NORMALIZATION_VERSION = "2026-07-14.1"
DOCUMENT_CONTEXT_CHARS = 600
DOMAIN_SHARE_WARNING = 0.10
DOMAIN_STORY_CAP = 100
DATASET_SCHEMA_VERSION = 5
PASSAGE_MAX_PARAGRAPHS = 8
PASSAGE_MAX_CHARS = 12_000
STORY_EXPANSION_VERSION = "seed-window-v4"
STORY_CONTEXT_BEFORE_PARAGRAPHS = 2
STORY_CONTEXT_AFTER_PARAGRAPHS = 3
STORY_MIN_CHARS = 350
STORY_MIN_SENTENCES = 3


# "auto" is resolved lazily by the semantic matcher so importing lightweight
# modules such as progress.py does not initialize PyTorch or CUDA.
DEVICE = os.environ.get("HOMETOWN_XR_DEVICE", "auto")


@dataclass(frozen=True)
class HardwareProfile:
    """Runtime settings that may differ between extractor workstations."""

    name: str
    workers: int
    candidate_batch_size: int
    inference_batch_size: int
    encoding_batch_size: int
    precision: str = "fp32"

    @property
    def stream_batch_size(self) -> int:
        """Backward-compatible name used by older scripts."""
        return self.candidate_batch_size


# All workstations use conservative seven-worker settings until identical
# real-source benchmarks prove a safe CPU-concurrency change. GPU-facing batch
# sizes scale by card and remain protected by adaptive CUDA OOM recovery.
HARDWARE_PROFILES = {
    "3080": HardwareProfile("3080", 7, 100, 800, 128, "fp32"),
    "4090": HardwareProfile("4090", 7, 150, 1_600, 256, "fp32"),
    "5090": HardwareProfile("5090", 7, 200, 2_400, 512, "fp32"),
}


def detect_hardware_profile() -> str:
    """Return the configured or GPU-detected hardware profile name."""
    requested = os.environ.get("HOMETOWN_XR_PROFILE", "auto").lower()
    if requested in HARDWARE_PROFILES:
        return requested
    if requested != "auto":
        valid = ", ".join(sorted(HARDWARE_PROFILES))
        raise ValueError(f"Unknown hardware profile {requested!r}; choose {valid}")

    try:
        import torch

        if torch.cuda.is_available():
            device_name = torch.cuda.get_device_name(0).lower()
            if "5090" in device_name:
                return "5090"
            if "4090" in device_name:
                return "4090"
            if "3080" in device_name:
                return "3080"
    except (ImportError, RuntimeError):
        pass

    return "3080"


def get_hardware_profile(name: str | None = None) -> HardwareProfile:
    """Resolve an explicit profile name or auto-detect the local GPU."""
    resolved = detect_hardware_profile() if not name or name == "auto" else name
    try:
        profile = HARDWARE_PROFILES[resolved]
    except KeyError as exc:
        valid = ", ".join(sorted(HARDWARE_PROFILES))
        raise ValueError(f"Unknown hardware profile {resolved!r}; choose {valid}") from exc

    if HARDWARE_OVERRIDE_PATH.exists():
        try:
            override = json.loads(HARDWARE_OVERRIDE_PATH.read_text(encoding="utf-8"))
            if override.get("profile") == profile.name:
                values = {
                    key: int(override[key])
                    for key in (
                        "workers",
                        "candidate_batch_size",
                        "inference_batch_size",
                        "encoding_batch_size",
                    )
                    if key in override
                }
                if override.get("precision") in {"fp32", "fp16"}:
                    values["precision"] = str(override["precision"])
                profile = replace(profile, **values)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return profile


_legacy_profile_name = os.environ.get("HOMETOWN_XR_PROFILE", "3080").lower()
if _legacy_profile_name not in HARDWARE_PROFILES:
    _legacy_profile_name = "3080"
_DEFAULT_PROFILE = HARDWARE_PROFILES[_legacy_profile_name]
MAX_WORKERS = _DEFAULT_PROFILE.workers
STREAM_BATCH_SIZE = _DEFAULT_PROFILE.stream_batch_size
INFERENCE_BATCH_SIZE = _DEFAULT_PROFILE.inference_batch_size
ENCODING_BATCH_SIZE = _DEFAULT_PROFILE.encoding_batch_size
