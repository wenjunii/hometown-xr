import subprocess
import sys
from pathlib import Path

from concepts import concept_anchor_language
from main import _schedule_order
from signatures import build_filter_signature, filter_contract


def test_filter_signature_is_stable_and_changes_with_behavior():
    assert build_filter_signature() == build_filter_signature()
    assert build_filter_signature(0.45) != build_filter_signature(0.46)
    contract = filter_contract()
    assert contract["semantic_model"]["revision"]
    assert contract["keywords"] == sorted(contract["keywords"])
    assert any(concept_anchor_language(anchor) == "zh" for anchor in contract["concept_anchors"])


def test_crawl_scheduling_supports_newest_oldest_and_balanced_order():
    crawls = ["oldest", "older", "newer", "newest"]
    assert _schedule_order(crawls, "oldest") == crawls
    assert _schedule_order(crawls, "newest") == list(reversed(crawls))
    assert _schedule_order(crawls, "round-robin") == [
        "newest",
        "oldest",
        "newer",
        "older",
    ]


def test_filter_cli_rejects_invalid_threshold_before_touching_state():
    result = subprocess.run(
        [sys.executable, "main.py", "filters", "--threshold", "2", "status"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "--threshold must be between 0 and 1" in result.stderr
