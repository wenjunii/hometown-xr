"""Cross-crawl scheduling policies."""

from __future__ import annotations

import math
from typing import Iterable


def yield_aware_order(
    crawl_ids: Iterable[str],
    summaries: Iterable[dict],
    prior_files: float = 200.0,
) -> list[str]:
    """Rank crawls by smoothed yield while retaining an exploration bonus."""
    ids = list(crawl_ids)
    by_crawl = {str(row.get("crawl_id")): row for row in summaries}
    completed_total = sum(
        max(0, int(by_crawl.get(crawl_id, {}).get("completed", 0)))
        for crawl_id in ids
    )
    matches_total = sum(
        max(0, int(by_crawl.get(crawl_id, {}).get("matches", 0)))
        for crawl_id in ids
    )
    global_yield = matches_total / completed_total if completed_total else 0.0
    baseline = max(global_yield, 0.01)

    def score(crawl_id: str) -> tuple[float, float, str]:
        row = by_crawl.get(crawl_id, {})
        completed = max(0, int(row.get("completed", 0)))
        matches = max(0, int(row.get("matches", 0)))
        total = max(completed + 1, int(row.get("total", completed + 1)))
        smoothed = (matches + prior_files * global_yield) / (completed + prior_files)
        exploration = baseline * math.sqrt(
            math.log(completed_total + prior_files + 1)
            / (completed + prior_files)
        )
        remaining_share = max(total - completed, 0) / total
        return smoothed + exploration + baseline * 0.05 * remaining_share, smoothed, crawl_id

    return sorted(ids, key=score, reverse=True)
