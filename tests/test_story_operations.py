import gzip
from datetime import datetime, timedelta, timezone

import pytest

from story_operations import (
    AdaptiveWorkerController,
    StoryFailureLedger,
    StoryRunTelemetry,
    read_story_run_state,
)


def _result(
    source_file="crawl-data/source.warc.wet.gz",
    *,
    status="failed",
    error="requests.exceptions.HTTPError: 503 Server Error",
):
    return {
        "source_file": source_file,
        "crawl_id": "CC-MAIN-2026-12",
        "status": status,
        "error": error,
        "missing_record_ids": ["record"],
        "stories": 0,
        "records_processed": 10,
    }


def test_failure_ledger_persists_cooldown_and_quarantine(tmp_path):
    path = tmp_path / "failures.jsonl.gz"
    ledger = StoryFailureLedger(
        path,
        max_attempts=2,
        retry_base_seconds=60,
        retry_max_seconds=60,
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    ledger.record_result(_result(), now=now)

    reloaded = StoryFailureLedger(path, max_attempts=2)
    assert reloaded.state("crawl-data/source.warc.wet.gz", now) == "cooldown"
    assert reloaded.state(
        "crawl-data/source.warc.wet.gz",
        now + timedelta(seconds=61),
    ) == "ready"
    assert reloaded.rows()[0]["category"] == "http_503"

    reloaded.record_result(_result(), now=now + timedelta(seconds=61))
    assert reloaded.state("crawl-data/source.warc.wet.gz") == "quarantined"
    assert reloaded.summary()["quarantined_sources"] == 1


def test_failure_ledger_clears_completed_source_and_resets_selected(tmp_path):
    path = tmp_path / "failures.jsonl.gz"
    ledger = StoryFailureLedger(path)
    ledger.record_result(_result("one"))
    ledger.record_result(_result("two"))

    removed = ledger.reset(source_files={"one"})
    ledger.record_result(_result("two", status="completed", error=None))

    assert [row["source_file"] for row in removed] == ["one"]
    assert ledger.rows() == []


def test_failure_ledger_refuses_corrupt_durable_state(tmp_path):
    path = tmp_path / "failures.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("not json\n")

    with pytest.raises(RuntimeError, match="failure ledger is unreadable"):
        StoryFailureLedger(path)


def test_adaptive_workers_ramp_up_and_back_off_on_server_pressure():
    controller = AdaptiveWorkerController(
        "auto",
        20,
        auto_min=2,
        auto_max=5,
        success_window=2,
    )
    success = _result(status="completed", error=None)

    assert not controller.observe(success)
    assert controller.observe(success)
    assert controller.target == 3
    assert controller.observe(_result())
    assert controller.target == 2
    assert controller.adjustments[-1]["reason"] == "http_503"


def test_run_telemetry_reports_active_sources_rate_and_completion(tmp_path):
    stories_dir = tmp_path / "stories"
    path = stories_dir / "_state" / "run-state.json"
    telemetry = StoryRunTelemetry(
        path,
        worker_mode="auto",
        configured_workers="auto",
        target_workers=3,
        maximum_workers=8,
        selected_sources=2,
    )

    telemetry.source_started("one", "crawl")
    telemetry.source_progress("one", 1_250, 600)
    active = read_story_run_state(stories_dir)
    assert active["status"] == "running"
    assert active["active_workers"] == 1
    assert active["active_sources"][0]["source_file"] == "one"
    assert active["records_processed"] == 1_250
    assert active["eligible_paragraphs"] == 600
    assert active["active_sources"][0]["elapsed_seconds"] is not None

    telemetry.source_finished(
        _result("one", status="completed", error=None),
        target_workers=4,
        adjustments=[],
    )
    telemetry.finish("interrupted")
    finished = read_story_run_state(stories_dir)
    assert finished["status"] == "interrupted"
    assert finished["finished_sources"] == 1
    assert finished["remaining_run_sources"] == 1
    assert finished["unresolved_sources"] == 0
    assert finished["target_workers"] == 4
