import json
from pathlib import Path

from progress import ProgressTracker
from recovery_campaign import (
    adopt_recovery_evidence,
    build_recovery_plan,
    recovery_evidence_status,
)
from signatures import build_filter_signature


def _fail(tracker, path, crawl, error):
    tracker.initialize_paths([path], crawl)
    tracker.mark_failed(path, error, retry_base_seconds=0)


def test_recovery_plan_is_bounded_and_adoption_resets_exact_verified_paths(tmp_path):
    tracker = ProgressTracker(tmp_path / "progress.db")
    signature = build_filter_signature()
    _fail(tracker, "crawl-data/a.wet.gz", "crawl", "503 Service Unavailable")
    _fail(tracker, "crawl-data/b.wet.gz", "crawl", "503 Service Unavailable")
    _fail(tracker, "crawl-data/c.wet.gz", "crawl", "BrokenProcessPool")

    plan = build_recovery_plan(
        ["http_503", "process_pool"],
        1,
        tracker=tracker,
        filter_signature=signature,
    )
    assert plan["total_sources"] == 2
    assert plan["sources_by_category"] == {"http_503": 1, "process_pool": 1}
    assert tracker.get_summary()["failed"] == 3

    recovered = plan["sources"][0]
    report = tmp_path / "recovery-report.json"
    report.write_text(
        json.dumps(
            {
                "campaign_type": "failure_recovery",
                "campaign_id": "campaign-one",
                "filter_signature": signature,
                "historical_state_changed": False,
                "sources": [
                    {
                        "file_path": recovered["file_path"],
                        "original_category": recovered["failure_category"],
                        "original_error_sha256": recovered["error_sha256"],
                        "recovered": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    status = recovery_evidence_status(
        report,
        tracker=tracker,
        current_signature=signature,
    )
    assert status["eligible_sources"] == [recovered["file_path"]]
    result = adopt_recovery_evidence(
        report,
        tracker=tracker,
        target_dir=tmp_path / "evidence",
    )
    assert result["rows_reset"] == 1
    assert Path(result["archived_report"]).exists()
    assert tracker.get_summary()["failed"] == 2
    assert tracker.get_summary()["pending"] == 1
