"""Stable failure categories shared by status and runtime recovery logic."""

from __future__ import annotations

import re


def classify_failure(error: str | None) -> str:
    value = (error or "").lower()
    if "process pool" in value or "brokenprocesspool" in value:
        return "process_pool"
    if "inference service" in value or "cuda" in value:
        return "inference"
    if "output commit" in value:
        return "output"
    if re.search(r"(?<!\d)429(?!\d)", value) or "too many requests" in value:
        return "http_429"
    for status in (500, 502, 503, 504):
        if re.search(rf"(?<!\d){status}(?!\d)", value):
            return f"http_{status}"
    if "timed out" in value or "timeout" in value:
        return "timeout"
    if any(
        term in value
        for term in (
            "connectionerror",
            "connection reset",
            "connection aborted",
            "protocolerror",
            "remotedisconnected",
            "nameresolutionerror",
            "failed to resolve",
            "getaddrinfo",
            "dns",
        )
    ):
        return "connection"
    if re.search(r"(?<!\d)404(?!\d)", value) or "not found" in value:
        return "http_404"
    return "other"


def is_transient_http_failure(error: str | None) -> bool:
    return classify_failure(error) in {
        "http_429",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "timeout",
        "connection",
    }
