import json

import pytest

from model_migration import approve_model_migration, build_model_migration_plan


def _snapshot(profile="3080", libraries=None):
    return {
        "profile": profile,
        "libraries": libraries
        or {
            "torch": "3.0.0",
            "transformers": "5.0.0",
            "sentence-transformers": "4.0.0",
        },
        "samples": [
            {
                "sample_id": "one",
                "semantic_score": 0.7,
                "concept_match": "home",
                "above_threshold": True,
            }
        ],
    }


def _ready_inputs(tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(_snapshot()), encoding="utf-8")
    candidates = {}
    for profile in ("3080", "4090", "5090"):
        path = tmp_path / f"candidate-{profile}.json"
        path.write_text(json.dumps(_snapshot(profile)), encoding="utf-8")
        candidates[profile] = path
    return {
        "baseline_path": baseline,
        "candidate_paths": candidates,
        "evaluation": {
            "labeled": 100,
            "baseline": {"ready": True, "holdout_ready": True, "holdout_labels": 20},
        },
        "filters": {"completed": 100, "current": 100, "unknown": 0, "stale": 0},
        "metrics": {"profiles": {"3080": {}, "4090": {}, "5090": {}}},
        "dependencies": {"valid": True},
    }


def test_model_migration_gate_requires_every_evidence_class(tmp_path):
    inputs = _ready_inputs(tmp_path)
    inputs["evaluation"] = {
        "labeled": 0,
        "baseline": {"ready": False, "holdout_ready": False, "holdout_labels": 0},
    }
    inputs["metrics"] = {"profiles": {"3080": {}}}

    result = build_model_migration_plan(**inputs)

    assert not result["ready"]
    blockers = {item["name"] for item in result["blockers"]}
    assert "human_evaluation" in blockers
    assert "real_workload_benchmarks" in blockers
    with pytest.raises(ValueError, match="blocked"):
        approve_model_migration(result, tmp_path / "evidence.json")


def test_model_migration_approval_records_only_fully_validated_plan(tmp_path):
    result = build_model_migration_plan(**_ready_inputs(tmp_path))

    evidence = approve_model_migration(result, tmp_path / "evidence.json")

    assert result["ready"]
    assert result["status"] == "ready_for_lock_update"
    assert all(item["safe"] for item in result["comparisons"].values())
    assert evidence["approved"]
    payload = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert payload["approved"]
    assert payload["blockers"] == []
