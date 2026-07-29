from project_health import build_health_checks, collect_story_storage_health
from story_packing import build_story_packs


def _payload():
    return {
        "git": {"dirty": False, "ahead": 0, "behind": 0},
        "crawler_lock_exists": False,
        "runtime": {
            "valid": True,
            "gpu": "NVIDIA GeForce RTX 3080",
            "cuda_runtime": "12.1",
            "errors": [],
        },
        "database": {"synchronized": True},
        "progress": {"processing": 0},
        "dependencies": {
            "profiles": {
                "valid": True,
                "security_policy": {"status": "migration_required", "review_by": "soon"},
            },
            "installed": {"valid": True},
        },
        "evaluation": {"labeled": 0, "baseline": {"ready": False, "minimum_labels": 100}},
        "filters": {"current": 0, "unknown": 10, "stale": 0},
        "metrics": {"profiles": {"3080": {}}},
        "model_baseline_exists": False,
    }


def test_health_checks_keep_readiness_gaps_as_warnings():
    checks = build_health_checks(_payload())

    assert not [check for check in checks if check["status"] == "fail"]
    assert {check["name"] for check in checks if check["status"] == "warning"} == {
        "dependency_security",
        "evaluation_baseline",
        "filter_signatures",
        "hardware_metrics",
        "model_baseline",
    }


def test_health_checks_fail_unsafe_handoff_state():
    payload = _payload()
    payload["crawler_lock_exists"] = True
    payload["database"]["synchronized"] = False

    checks = build_health_checks(payload)

    assert {check["name"] for check in checks if check["status"] == "fail"} == {
        "crawler_lock",
        "database_checkpoint",
    }


def test_story_health_accepts_verified_packs_without_local_fragments(tmp_path):
    stories = tmp_path / "stories"
    records = stories / "_records"
    records.mkdir(parents=True)
    (records / f"{'a' * 20}.jsonl.gz").write_bytes(b"exact fragment bytes")
    build_story_packs(stories)
    (records / f"{'a' * 20}.jsonl.gz").unlink()

    fragments, packs = collect_story_storage_health(stories)

    assert fragments["valid"]
    assert fragments["restorable_from_packs"]
    assert packs["valid"]
