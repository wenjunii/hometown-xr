"""Export compressed JSONL results to disk-sorted Markdown files."""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from config import DATA_DIR, OUTPUT_DIR

logger = logging.getLogger(__name__)
DEFAULT_EXPORT_DIR = DATA_DIR / "exports"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


def _write_record(handle, index: int, record: dict) -> None:
    score = float(record.get("semantic_score", 0))
    keywords = ", ".join(record.get("matched_keywords", []))
    concept = record.get("concept_match", "N/A")
    url = record.get("url", "#")
    date = record.get("warc_date", "Unknown date")
    crawl = record.get("crawl_id", "Unknown")
    source_file = record.get("source_file", "Legacy output")
    text = record.get("paragraph", "")

    handle.write(f"### {index}. Match Score: {score:.3f}\n")
    handle.write(f"- **Keywords:** `{keywords}`\n")
    handle.write(f"- **Concept Anchor:** '{concept}'\n")
    handle.write(f"- **Source URL:** [{url}]({url})\n")
    handle.write(f"- **Capture Date:** {date}\n")
    handle.write(f"- **Crawl Dataset:** `{crawl}`\n")
    handle.write(f"- **Source File:** `{source_file}`\n\n")
    handle.write("\n".join(f"> {line}" for line in text.splitlines()))
    handle.write("\n\n---\n\n")


def export_to_markdown(
    output_dir: str | Path = OUTPUT_DIR,
    export_dir: str | Path = DEFAULT_EXPORT_DIR,
) -> dict[str, int]:
    """Use temporary SQLite storage so export memory stays bounded."""
    output_path = Path(output_dir)
    export_path = Path(export_dir)
    export_path.mkdir(parents=True, exist_ok=True)
    files = list(output_path.glob("*/*.jsonl.gz"))
    if not files:
        logger.warning("No JSONL output shards found under %s", output_path)
        return {}

    descriptor, temp_name = tempfile.mkstemp(
        prefix="hometown-xr-export-", suffix=".db", dir=export_path
    )
    os.close(descriptor)
    temp_db = Path(temp_name)
    counts: dict[str, int] = {}

    try:
        conn = sqlite3.connect(str(temp_db))
        try:
            conn.execute("CREATE TABLE records (language TEXT, score REAL, payload TEXT)")
            batch = []
            for file_path in files:
                with gzip.open(file_path, "rt", encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        record = json.loads(line)
                        batch.append(
                            (
                                record.get("language", "unknown"),
                                float(record.get("semantic_score", 0)),
                                json.dumps(record, ensure_ascii=False),
                            )
                        )
                        if len(batch) >= 1000:
                            conn.executemany("INSERT INTO records VALUES (?, ?, ?)", batch)
                            batch.clear()
            if batch:
                conn.executemany("INSERT INTO records VALUES (?, ?, ?)", batch)
            conn.execute("CREATE INDEX idx_records_language_score ON records(language, score DESC)")
            conn.commit()

            language_rows = conn.execute(
                "SELECT language, COUNT(*) FROM records GROUP BY language"
            ).fetchall()
            generated: set[Path] = set()
            for language, count in language_rows:
                safe_language = _SAFE_NAME.sub("_", language) or "unknown"
                destination = export_path / f"matches_{safe_language}.md"
                temporary = destination.with_suffix(".md.tmp")
                with temporary.open("w", encoding="utf-8") as handle:
                    handle.write("# Extracted Concepts: Home & Belonging\n\n")
                    handle.write(f"**Language:** `{language}`\n")
                    handle.write(f"**Total Matches:** {count}\n\n---\n\n")
                    rows = conn.execute(
                        "SELECT payload FROM records WHERE language = ? ORDER BY score DESC",
                        (language,),
                    )
                    for index, (payload,) in enumerate(rows, 1):
                        _write_record(handle, index, json.loads(payload))
                os.replace(temporary, destination)
                generated.add(destination)
                counts[language] = count

            for old_export in export_path.glob("matches_*.md"):
                if old_export not in generated:
                    old_export.unlink()
        finally:
            conn.close()
    finally:
        temp_db.unlink(missing_ok=True)

    logger.info("Exported %s records across %s languages", sum(counts.values()), len(counts))
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT_DIR)
    args = parser.parse_args()
    export_to_markdown(args.output, args.export)


if __name__ == "__main__":
    main()
