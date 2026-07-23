import gzip
import json

from export_md import build_match_rank_index, export_to_markdown


def _write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def test_match_export_ranks_are_stable_for_equal_scores(tmp_path):
    output_dir = tmp_path / "output"
    export_dir = tmp_path / "exports"
    later = {
        "record_id": "later",
        "language": "en",
        "semantic_score": 0.8,
        "warc_date": "2026-02-01",
        "paragraph": "Later capture.",
    }
    earlier = {
        "record_id": "earlier",
        "language": "en",
        "semantic_score": 0.8,
        "warc_date": "2026-01-01",
        "paragraph": "Earlier capture.",
    }
    _write_record(output_dir / "z" / "later.jsonl.gz", later)
    _write_record(output_dir / "a" / "earlier.jsonl.gz", earlier)

    ranks = build_match_rank_index(output_dir)
    export_to_markdown(output_dir, export_dir)
    markdown = (export_dir / "matches_en.md").read_text(encoding="utf-8")

    assert ranks == {
        "earlier": ("en", 1),
        "later": ("en", 2),
    }
    assert markdown.index("### 1. Match Score") < markdown.index("Earlier capture.")
    assert markdown.index("Earlier capture.") < markdown.index("### 2. Match Score")
    assert markdown.index("### 2. Match Score") < markdown.index("Later capture.")
