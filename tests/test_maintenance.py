from maintenance import assemble_maintenance_plan


def _plan(**overrides):
    values = {
        "profile_name": "3080",
        "progress": {
            "total_files": 100,
            "completed": 60,
            "pending": 39,
            "processing": 0,
        },
        "failures": {
            "failed": 1,
            "retryable_now": 1,
            "attempts_exhausted": 0,
            "categories": {"http_503": {"count": 1, "examples": []}},
        },
        "evaluation": {
            "ready": False,
            "remaining_to_target": 100,
            "steps": [],
            "insufficient_queues": [{"split": "holdout", "prediction": "rejected"}],
            "browser_command": "python main.py evaluation serve --open-browser",
        },
        "filters": {
            "completed": 60,
            "current": 0,
            "unknown": 60,
            "stale": 0,
        },
        "audit": {"total_sources": 10},
        "dependencies": {
            "security_policy": {
                "status": "migration_required",
                "review_by": "2026-08-31",
                "days_until_review": 33,
            }
        },
        "metrics": {"profiles": {"3080": {}}},
        "model_baseline_exists": True,
    }
    values.update(overrides)
    return assemble_maintenance_plan(**values)


def test_plan_keeps_model_migration_blocked_without_evidence():
    result = _plan()

    assert result["read_only"]
    assert not result["model_migration"]["ready"]
    assert result["model_migration"]["missing_profiles"] == ["4090", "5090"]
    assert "human evaluation" in result["model_migration"]["blockers"][0]
    assert not result["evaluation"]["automatic_labeling_allowed"]
    assert result["crawl"]["failure_waves"][0]["bounded_reset_limit"] == 1


def test_plan_reports_ready_when_no_work_or_validation_gaps_remain():
    result = _plan(
        progress={
            "total_files": 100,
            "completed": 100,
            "pending": 0,
            "processing": 0,
        },
        failures={
            "failed": 0,
            "retryable_now": 0,
            "attempts_exhausted": 0,
            "categories": {},
        },
        evaluation={"ready": True, "remaining_to_target": 0},
        filters={
            "completed": 100,
            "current": 100,
            "unknown": 0,
            "stale": 0,
        },
        dependencies={"security_policy": {"status": "current"}},
        metrics={"profiles": {"3080": {}, "4090": {}, "5090": {}}},
    )

    assert result["status"] == "ready"
    assert result["actions"] == []
    assert not result["model_migration"]["required"]
