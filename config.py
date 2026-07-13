"""
Central configuration for the Common Crawl Home/Belonging Extractor.
"""

import os
from pathlib import Path

# ── Project Paths ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = DATA_DIR / "output"
MODELS_DIR = DATA_DIR / "models"
DB_PATH = DATA_DIR / "progress.db"

# Ensure directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
MODELS_DIR.mkdir(parents=True, exist_ok=True)

# ── Common Crawl Settings ────────────────────────────────────────────────────
CC_BASE_URL = "https://data.commoncrawl.org/"
DEFAULT_CRAWL_ID = "CC-MAIN-2026-12"

# ── Matching Settings ────────────────────────────────────────────────────────
# Minimum cosine similarity score to accept a paragraph as a match
SEMANTIC_THRESHOLD = 0.45

# Minimum paragraph length in characters (filters out navigation text, short labels)
MIN_PARAGRAPH_LENGTH = 150

# Maximum paragraph length in characters (filters out extremely long blocks)
MAX_PARAGRAPH_LENGTH = 5000

# Batch size for sentence-transformer encoding
# Higher values (128+) recommended for GPU
ENCODING_BATCH_SIZE = 128

# ── Narrative Voice Filter ───────────────────────────────────────────────────
# Enable the narrative voice filter (Stage 3) to prefer personal stories
NARRATIVE_FILTER_ENABLED = True

# Minimum number of first-person / narrative indicators required
# in a paragraph to pass the narrative filter
MIN_NARRATIVE_INDICATORS = 8

# ── Semantic Model ───────────────────────────────────────────────────────────
# Multilingual sentence transformer — supports 50+ languages, ~500 MB
SEMANTIC_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# ── FastText Language Detection ──────────────────────────────────────────────
FASTTEXT_MODEL_URL = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.bin"
FASTTEXT_MODEL_PATH = MODELS_DIR / "lid.176.bin"

# Minimum confidence for language detection
LANG_DETECTION_THRESHOLD = 0.5

# ── Network Settings ─────────────────────────────────────────────────────────
HTTP_TIMEOUT = 60  # seconds
HTTP_RETRIES = 3
HTTP_BACKOFF_FACTOR = 1.0

# ── Processing Settings ─────────────────────────────────────────────────────
# Number of characters from a paragraph used for language detection
LANG_DETECT_CHARS = 500

# Device for semantic matching ('cuda', 'mps', or 'cpu')
import torch
if torch.cuda.is_available():
    DEVICE = "cuda"
elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
    DEVICE = "mps"
else:
    DEVICE = "cpu"

# Multiprocessing settings
# 7 workers optimized for performance and stability
MAX_WORKERS = 7
# Max number of paragraphs to send to matcher in one go (prevents memory spikes)
MAX_PARAGRAPHS_PER_BATCH = 5000
