"""
Catalog of all Common Crawl datasets from 2008 to present.

Two eras of data:
  - Legacy (2008-2012): ARC format, raw HTML, no WET files
  - Modern (2013-present): WARC/WAT/WET format, pre-extracted text

Modern crawls are auto-discovered from the Common Crawl index API,
so new crawls are picked up automatically without code changes.
"""

import logging
import threading
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

# URL that lists all available modern crawls dynamically
COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"


@dataclass
class CrawlInfo:
    """Metadata about a single crawl."""

    crawl_id: str
    era: str  # "legacy" or "modern"
    format: str  # "arc" or "wet"
    base_url: str  # Base URL for accessing files
    paths_file: str  # Path to the file listing (or empty for legacy)
    notes: str = ""


# ── Legacy Crawls (2008-2012): ARC format ────────────────────────────────────
# These contain raw HTML — text must be extracted from HTML content.
# Stored under different S3 paths than modern crawls.
LEGACY_CRAWLS = [
    CrawlInfo(
        crawl_id="CC-CRAWL-001",
        era="legacy",
        format="arc",
        base_url="https://data.commoncrawl.org/",
        paths_file="crawl-001/",
        notes="2008-2010 crawl, Nutch-based ARC format",
    ),
    CrawlInfo(
        crawl_id="CC-CRAWL-002",
        era="legacy",
        format="arc",
        base_url="https://data.commoncrawl.org/",
        paths_file="crawl-002/",
        notes="2009-2010 crawl, Nutch-based ARC format",
    ),
    CrawlInfo(
        crawl_id="CC-2012",
        era="legacy",
        format="arc",
        base_url="https://data.commoncrawl.org/",
        paths_file="parse-output/",
        notes="2012 crawl, commoncrawl-crawler ARC format",
    ),
]

# Cache for the live crawl list (fetched once per session)
_modern_crawls_cache: list[str] | None = None
_modern_crawls_lock = threading.Lock()


def _fetch_modern_crawl_ids() -> list[str]:
    """
    Fetch the list of modern crawl IDs from the Common Crawl index API.

    This is a live query — it automatically picks up new crawls
    as they are published by Common Crawl.

    Returns crawl IDs in reverse chronological order (newest first).
    """
    global _modern_crawls_cache

    if _modern_crawls_cache is not None:
        return _modern_crawls_cache

    with _modern_crawls_lock:
        if _modern_crawls_cache is not None:
            return _modern_crawls_cache

        logger.info(f"Fetching available crawls from {COLLINFO_URL}")
        try:
            response = requests.get(COLLINFO_URL, timeout=30)
            response.raise_for_status()
            data = response.json()

            # Filter out legacy ARC file indexes that don't have WET files
            legacy_indexes = {
                "CC-MAIN-2012",
                "CC-MAIN-2009-2010",
                "CC-MAIN-2008-2009",
            }

            # Each entry has an "id" like "CC-MAIN-2026-12".
            crawl_ids = [
                entry["id"]
                for entry in data
                if "id" in entry and entry["id"] not in legacy_indexes
            ]
            logger.info(f"Found {len(crawl_ids)} modern crawls available")

            _modern_crawls_cache = crawl_ids
            return crawl_ids

        except Exception as e:
            logger.warning(
                f"Failed to fetch live crawl list: {e}. Using hardcoded fallback list."
            )
            _modern_crawls_cache = _FALLBACK_MODERN_CRAWLS
            return _FALLBACK_MODERN_CRAWLS


# Hardcoded fallback in case the API is unreachable
_FALLBACK_MODERN_CRAWLS = [
    "CC-MAIN-2026-12",
    "CC-MAIN-2026-08",
    "CC-MAIN-2026-04",
    "CC-MAIN-2025-51",
    "CC-MAIN-2025-47",
    "CC-MAIN-2025-43",
    "CC-MAIN-2025-38",
    "CC-MAIN-2025-33",
    "CC-MAIN-2025-30",
    "CC-MAIN-2025-26",
    "CC-MAIN-2025-21",
    "CC-MAIN-2025-18",
    "CC-MAIN-2025-13",
    "CC-MAIN-2025-08",
    "CC-MAIN-2025-05",
    "CC-MAIN-2024-51",
    "CC-MAIN-2024-46",
    "CC-MAIN-2024-42",
    "CC-MAIN-2024-38",
    "CC-MAIN-2024-33",
    "CC-MAIN-2024-30",
    "CC-MAIN-2024-26",
    "CC-MAIN-2024-22",
    "CC-MAIN-2024-18",
    "CC-MAIN-2024-10",
    "CC-MAIN-2023-50",
    "CC-MAIN-2023-40",
    "CC-MAIN-2023-23",
    "CC-MAIN-2023-14",
    "CC-MAIN-2023-06",
    "CC-MAIN-2022-49",
    "CC-MAIN-2022-40",
    "CC-MAIN-2022-33",
    "CC-MAIN-2022-27",
    "CC-MAIN-2022-21",
    "CC-MAIN-2022-05",
    "CC-MAIN-2021-49",
    "CC-MAIN-2021-43",
    "CC-MAIN-2021-39",
    "CC-MAIN-2021-31",
    "CC-MAIN-2021-25",
    "CC-MAIN-2021-21",
    "CC-MAIN-2021-17",
    "CC-MAIN-2021-10",
    "CC-MAIN-2021-04",
    "CC-MAIN-2020-50",
    "CC-MAIN-2020-45",
    "CC-MAIN-2020-40",
    "CC-MAIN-2020-34",
    "CC-MAIN-2020-29",
    "CC-MAIN-2020-24",
    "CC-MAIN-2020-16",
    "CC-MAIN-2020-10",
    "CC-MAIN-2020-05",
    "CC-MAIN-2019-51",
    "CC-MAIN-2019-47",
    "CC-MAIN-2019-43",
    "CC-MAIN-2019-39",
    "CC-MAIN-2019-35",
    "CC-MAIN-2019-30",
    "CC-MAIN-2019-26",
    "CC-MAIN-2019-22",
    "CC-MAIN-2019-18",
    "CC-MAIN-2019-13",
    "CC-MAIN-2019-09",
    "CC-MAIN-2019-04",
    "CC-MAIN-2018-51",
    "CC-MAIN-2018-47",
    "CC-MAIN-2018-43",
    "CC-MAIN-2018-39",
    "CC-MAIN-2018-34",
    "CC-MAIN-2018-30",
    "CC-MAIN-2018-26",
    "CC-MAIN-2018-22",
    "CC-MAIN-2018-17",
    "CC-MAIN-2018-13",
    "CC-MAIN-2018-09",
    "CC-MAIN-2018-05",
    "CC-MAIN-2017-51",
    "CC-MAIN-2017-47",
    "CC-MAIN-2017-43",
    "CC-MAIN-2017-39",
    "CC-MAIN-2017-34",
    "CC-MAIN-2017-30",
    "CC-MAIN-2017-26",
    "CC-MAIN-2017-22",
    "CC-MAIN-2017-17",
    "CC-MAIN-2017-13",
    "CC-MAIN-2017-09",
    "CC-MAIN-2017-04",
    "CC-MAIN-2016-50",
    "CC-MAIN-2016-44",
    "CC-MAIN-2016-40",
    "CC-MAIN-2016-36",
    "CC-MAIN-2016-30",
    "CC-MAIN-2016-26",
    "CC-MAIN-2016-22",
    "CC-MAIN-2016-18",
    "CC-MAIN-2016-07",
    "CC-MAIN-2015-48",
    "CC-MAIN-2015-40",
    "CC-MAIN-2015-35",
    "CC-MAIN-2015-32",
    "CC-MAIN-2015-27",
    "CC-MAIN-2015-22",
    "CC-MAIN-2015-18",
    "CC-MAIN-2015-14",
    "CC-MAIN-2015-11",
    "CC-MAIN-2015-06",
    "CC-MAIN-2014-52",
    "CC-MAIN-2014-49",
    "CC-MAIN-2014-42",
    "CC-MAIN-2014-41",
    "CC-MAIN-2014-35",
    "CC-MAIN-2014-23",
    "CC-MAIN-2014-15",
    "CC-MAIN-2014-10",
    "CC-MAIN-2013-48",
    "CC-MAIN-2013-20",
]


def get_modern_crawls() -> list[str]:
    """Get all modern crawl IDs (newest first). Auto-discovers new crawls."""
    return _fetch_modern_crawl_ids()


def get_crawl_info(crawl_id: str) -> CrawlInfo:
    """Get metadata for a crawl by its ID."""
    # Check legacy crawls
    for crawl in LEGACY_CRAWLS:
        if crawl.crawl_id == crawl_id:
            return crawl

    # Check modern crawls (live from API)
    modern_ids = get_modern_crawls()
    if crawl_id in modern_ids:
        return CrawlInfo(
            crawl_id=crawl_id,
            era="modern",
            format="wet",
            base_url="https://data.commoncrawl.org/",
            paths_file=f"crawl-data/{crawl_id}/wet.paths.gz",
        )

    raise ValueError(
        f"Unknown crawl ID: {crawl_id}. Use 'python main.py list' to see all available crawls."
    )


def get_all_crawl_ids() -> list[str]:
    """Get all crawl IDs in chronological order (oldest first)."""
    legacy_ids = [c.crawl_id for c in LEGACY_CRAWLS]
    modern_ids = list(reversed(get_modern_crawls()))  # Reverse to oldest first
    return legacy_ids + modern_ids


def is_legacy_crawl(crawl_id: str) -> bool:
    """Check if a crawl uses the legacy ARC format."""
    return any(c.crawl_id == crawl_id for c in LEGACY_CRAWLS)
