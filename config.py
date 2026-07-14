"""Central configuration for the Hometown XR Common Crawl extractor."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = DATA_DIR / "output"
MODELS_DIR = DATA_DIR / "models"
DB_PATH = DATA_DIR / "progress.db"
RUN_LOCK_PATH = DATA_DIR / ".crawler.lock"

for directory in (DATA_DIR, OUTPUT_DIR, MODELS_DIR):
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


# Language detection
FASTTEXT_MODEL_URL = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.bin"
FASTTEXT_MODEL_PATH = MODELS_DIR / "lid.176.bin"
LANG_DETECTION_THRESHOLD = 0.5
LANG_DETECT_CHARS = 500


# Network
HTTP_TIMEOUT = 60
HTTP_RETRIES = 3
HTTP_BACKOFF_FACTOR = 1.0


# Retry and lease behavior
MAX_FILE_ATTEMPTS = 4
RETRY_BASE_SECONDS = 300
RETRY_MAX_SECONDS = 21_600
LEASE_TIMEOUT_SECONDS = 600
HEARTBEAT_INTERVAL_SECONDS = 30


# "auto" is resolved lazily by the semantic matcher so importing lightweight
# modules such as progress.py does not initialize PyTorch or CUDA.
DEVICE = os.environ.get("HOMETOWN_XR_DEVICE", "auto")


@dataclass(frozen=True)
class HardwareProfile:
    """Runtime settings that may differ between extractor workstations."""

    name: str
    workers: int
    stream_batch_size: int
    encoding_batch_size: int


# Both machines currently use the proven seven-worker settings. Keeping the
# profiles explicit makes future tuning a configuration change, not a code fork.
HARDWARE_PROFILES = {
    "3080": HardwareProfile("3080", 7, 200, 128),
    "4090": HardwareProfile("4090", 7, 200, 128),
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
        return HARDWARE_PROFILES[resolved]
    except KeyError as exc:
        valid = ", ".join(sorted(HARDWARE_PROFILES))
        raise ValueError(f"Unknown hardware profile {resolved!r}; choose {valid}") from exc


_legacy_profile_name = os.environ.get("HOMETOWN_XR_PROFILE", "3080").lower()
if _legacy_profile_name not in HARDWARE_PROFILES:
    _legacy_profile_name = "3080"
_DEFAULT_PROFILE = HARDWARE_PROFILES[_legacy_profile_name]
MAX_WORKERS = _DEFAULT_PROFILE.workers
STREAM_BATCH_SIZE = _DEFAULT_PROFILE.stream_batch_size
ENCODING_BATCH_SIZE = _DEFAULT_PROFILE.encoding_batch_size
