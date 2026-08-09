import operations_dashboard
from operations_dashboard import _CSS, _HTML, collect_operations_status


class _Tracker:
    def get_summary(self):
        return {
            "total_files": 100,
            "completed": 60,
            "failed": 3,
            "pending": 37,
            "processing": 0,
            "retryable": 2,
        }

    def get_failure_summary(self, examples_per_category=0):
        assert examples_per_category == 0
        return {
            "attempts_exhausted": 1,
            "categories": {"http_503": {"count": 3}},
        }


def test_operations_status_is_read_only_and_dashboard_is_responsive(monkeypatch):
    monkeypatch.setattr(operations_dashboard, "ProgressTracker", _Tracker)
    monkeypatch.setattr(
        operations_dashboard,
        "evaluation_campaign",
        lambda include_samples=False: {
            "completed": 4,
            "target": 100,
            "remaining": 96,
            "queued": 20,
            "progress": 0.04,
            "blocked_phases": [],
            "phases": [],
        },
    )
    monkeypatch.setattr(
        operations_dashboard,
        "portable_evidence_status",
        lambda: {
            "profiles": {},
            "complete_model_profiles": [],
            "complete_workload_profiles": [],
        },
    )
    monkeypatch.setattr(
        operations_dashboard,
        "read_story_run_state",
        lambda: {"status": "completed", "stories_written": 3416},
    )
    monkeypatch.setattr(
        operations_dashboard,
        "story_failure_status",
        lambda: {"sources": 0, "categories": {}},
    )
    monkeypatch.setattr(
        operations_dashboard,
        "collect_maintenance_plan",
        lambda profile: {"actions": [{"priority": 1, "area": "crawl"}]},
    )
    monkeypatch.setattr(operations_dashboard, "compare_profiles", lambda: {"profiles": {}})
    monkeypatch.setattr(operations_dashboard, "latest_metrics", lambda: {})
    monkeypatch.setattr(
        operations_dashboard,
        "git_health",
        lambda: {"branch": "main", "commit": "a" * 40},
    )
    operations_dashboard._CACHE["payload"] = None

    result = collect_operations_status("3080", cache_seconds=0)
    assert result["read_only"]
    assert result["crawl"]["completion_pct"] == 60.0
    assert result["crawl"]["failure_categories"] == {"http_503": 3}
    assert "data-panel=\"evidence\"" in _HTML
    assert "src=\"/status.js\"" in _HTML
    assert "@media(max-width:760px)" in _CSS
