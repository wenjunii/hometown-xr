"""
Export extracted JSONL records to readable Markdown files.

Reads all compressed JSONL files in the data/output directory and 
generates a beautifully formatted Markdown file for each language.
"""

import argparse
import glob
import gzip
import json
import logging
from pathlib import Path
from collections import defaultdict

from config import DATA_DIR, OUTPUT_DIR

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = OUTPUT_DIR
DEFAULT_EXPORT_DIR = DATA_DIR / "exports"


def export_to_markdown(output_dir=DEFAULT_OUTPUT_DIR, export_dir=DEFAULT_EXPORT_DIR):
    """
    Export all .jsonl.gz files to formatted Markdown files.
    """
    output_path = Path(output_dir)
    export_path = Path(export_dir)
    export_path.mkdir(parents=True, exist_ok=True)

    if not output_path.exists():
        logger.error(f"Output directory {output_dir} not found.")
        return

    # Group records by language
    logger.info("Scanning for extracted records...")
    records_by_lang = defaultdict(list)
    
    file_pattern = str(output_path / "**" / "*.jsonl.gz")
    files = glob.glob(file_pattern, recursive=True)
    
    if not files:
        logger.warning("No .jsonl.gz files found to export.")
        return

    for file in files:
        try:
            with gzip.open(file, "rt", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    lang = record.get("language", "unknown")
                    records_by_lang[lang].append(record)
        except Exception as e:
            logger.error(f"Error reading {file}: {e}")

    # Write Markdown files per language
    for lang, records in records_by_lang.items():
        # Sort by semantic score (highest first)
        records.sort(key=lambda x: x.get("semantic_score", 0), reverse=True)
        
        md_file = export_path / f"matches_{lang}.md"
        logger.info(f"Writing {len(records)} matches to {md_file}")
        
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(f"# Extracted Concepts: Home & Belonging\n\n")
            f.write(f"**Language:** `{lang}`\n")
            f.write(f"**Total Matches:** {len(records)}\n\n")
            f.write(f"---\n\n")
            
            for i, r in enumerate(records, 1):
                score = r.get("semantic_score", 0)
                kw = ", ".join(r.get("matched_keywords", []))
                concept = r.get("concept_match", "N/A")
                url = r.get("url", "#")
                date = r.get("warc_date", "Unknown date")
                crawl = r.get("crawl_id", "Unknown")
                text = r.get("paragraph", "")
                
                f.write(f"### {i}. Match Score: {score:.3f}\n")
                f.write(f"- **Keywords:** `{kw}`\n")
                f.write(f"- **Concept Anchor:** '{concept}'\n")
                f.write(f"- **Source:** [{url}]({url})\n")
                f.write(f"- **Capture Date:** {date}\n")
                f.write(f"- **Crawl Dataset:** `{crawl}`\n\n")
                
                # Format paragraph as blockquote
                blockquote = "\n".join(f"> {line}" for line in text.split("\n"))
                f.write(f"{blockquote}\n\n")
                f.write(f"---\n\n")
                
    logger.info(f"✅ Export complete. Markdown files are in '{export_dir}/'")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Export JSONL results to Markdown.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_DIR, help="Directory with .jsonl.gz files")
    parser.add_argument("--export", default=DEFAULT_EXPORT_DIR, help="Directory to save .md files")
    args = parser.parse_args()
    
    export_to_markdown(args.output, args.export)
