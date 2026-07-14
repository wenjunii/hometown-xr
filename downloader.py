"""
WET/ARC file list fetching and HTTP streaming.

Handles:
- Modern crawls (2013+): Downloads wet.paths.gz for file listing
- Legacy crawls (2008-2012): Lists ARC files from S3 directory
- Streaming individual files via HTTP
"""

import gzip
import logging
from contextlib import contextmanager
from typing import Iterator

import boto3
import requests
from botocore.exceptions import ClientError, NoCredentialsError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import CC_BASE_URL, HTTP_BACKOFF_FACTOR, HTTP_RETRIES, HTTP_TIMEOUT
from crawl_catalog import CrawlInfo

logger = logging.getLogger(__name__)


def _make_session() -> requests.Session:
    """Create a requests Session with retry logic."""
    session = requests.Session()
    retry = Retry(
        total=HTTP_RETRIES,
        backoff_factor=HTTP_BACKOFF_FACTOR,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_session = _make_session()


def fetch_file_paths(crawl_info: CrawlInfo) -> list[str]:
    """
    Get the list of data file paths for a crawl.

    For modern crawls: downloads wet.paths.gz
    For legacy crawls: lists ARC files from S3
    """
    if crawl_info.era == "modern":
        return _fetch_wet_paths(crawl_info)
    else:
        return _fetch_arc_paths(crawl_info)


def _fetch_wet_paths(crawl_info: CrawlInfo) -> list[str]:
    """Download and parse the wet.paths.gz file for a modern crawl."""
    url = f"{crawl_info.base_url}{crawl_info.paths_file}"
    logger.info(f"Fetching WET file list from {url}")

    try:
        response = _session.get(url, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
    except Exception as e:
        logger.warning(f"Failed to fetch WET file list from {url}: {e}")
        return []

    decompressed = gzip.decompress(response.content)
    paths = decompressed.decode("utf-8").strip().split("\n")
    paths = [p.strip() for p in paths if p.strip()]

    logger.info(f"Found {len(paths)} WET files for {crawl_info.crawl_id}")
    return paths


def _fetch_arc_paths(crawl_info: CrawlInfo) -> list[str]:
    """
    List ARC files from a legacy crawl's S3 directory using boto3.

    This now requires AWS credentials because Common Crawl disabled
    anonymous directory listing on their main S3 buckets.
    """
    base_prefix = crawl_info.paths_file
    logger.info(
        f"Listing ARC files for legacy crawl {crawl_info.crawl_id} "
        f"(prefix: {base_prefix}) using AWS credentials"
    )

    arc_paths = []

    try:
        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket="commoncrawl", Prefix=base_prefix)

        for page in pages:
            if "Contents" not in page:
                continue
            for obj in page["Contents"]:
                key = obj["Key"]
                if key and (key.endswith(".arc.gz") or key.endswith(".arc")):
                    arc_paths.append(key)

    except NoCredentialsError:
        logger.error(
            "AWS credentials not found. You must set your AWS credentials "
            "(e.g., via 'aws configure' or AWS_ACCESS_KEY_ID env vars) "
            "to list legacy ARC path files from Common Crawl's S3 bucket."
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "AccessDenied":
            logger.error(
                "Access Denied to S3. Ensure your AWS IAM user has s3:ListBucket permissions for 'commoncrawl'."
            )
        else:
            logger.error(f"Failed to list S3 directory: {e}")
    except Exception as e:
        logger.error(f"Unexpected error listing S3 for legacy crawl: {e}")

    logger.info(f"Found {len(arc_paths)} ARC files for {crawl_info.crawl_id}")
    return arc_paths


@contextmanager
def stream_file(file_path: str, crawl_info: CrawlInfo) -> Iterator[object]:
    """
    Open an HTTP stream to a WET or ARC file.

    Args:
        file_path: Relative path to the file
        crawl_info: Crawl metadata

    Yields:
        A file-like stream object. The HTTP response is always closed.
    """
    # Modern crawls use data.commoncrawl.org
    # Legacy crawls also accessible via data.commoncrawl.org
    url = f"{CC_BASE_URL}{file_path}"
    logger.debug(f"Streaming file: {url}")

    response = _session.get(url, timeout=HTTP_TIMEOUT, stream=True)
    try:
        response.raise_for_status()
        response.raw.decode_content = True
        yield response.raw
    finally:
        response.close()
