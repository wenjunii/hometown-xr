"""Stream output shards and display the highest-scoring matches."""

from __future__ import annotations

import argparse
import gzip
import heapq
import json
import sys
from collections import Counter
from pathlib import Path

from config import OUTPUT_DIR


def top_matches(output_dir: str | Path, limit: int) -> tuple[list[dict], int, Counter]:
    heap: list[tuple[float, int, dict]] = []
    languages: Counter[str] = Counter()
    total = 0
    sequence = 0

    for path in Path(output_dir).glob("*/*.jsonl.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                total += 1
                sequence += 1
                languages[record.get("language", "unknown")] += 1
                item = (float(record.get("semantic_score", 0)), sequence, record)
                if len(heap) < limit:
                    heapq.heappush(heap, item)
                elif item[0] > heap[0][0]:
                    heapq.heapreplace(heap, item)

    records = [item[2] for item in sorted(heap, reverse=True)]
    return records, total, languages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    records, total, languages = top_matches(args.output, args.limit)
    print(f"\nTotal matches: {total}")
    print(f"Languages: {dict(sorted(languages.items()))}")
    print(f"\nTOP {len(records)} MATCHES BY SEMANTIC SCORE")
    print("=" * 70)
    for record in records:
        print(
            f"\nScore: {record.get('semantic_score', 0):.3f} | "
            f"Lang: {record.get('language', 'unknown')} | "
            f"Keywords: {record.get('matched_keywords', [])[:3]}"
        )
        print(f"URL: {record.get('url', '')[:80]}")
        print(f"Paragraph: {record.get('paragraph', '')[:250]}...")
        print("-" * 70)


if __name__ == "__main__":
    main()
