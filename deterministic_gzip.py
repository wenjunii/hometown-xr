"""Shared byte-stable gzip output helpers."""

from __future__ import annotations

import gzip
from typing import BinaryIO


def gzip_binary_writer(
    fileobj: BinaryIO,
    *,
    compresslevel: int = 9,
) -> gzip.GzipFile:
    """Return a gzip writer without timestamps or temporary filenames."""
    return gzip.GzipFile(
        filename="",
        fileobj=fileobj,
        mode="wb",
        compresslevel=compresslevel,
        mtime=0,
    )
