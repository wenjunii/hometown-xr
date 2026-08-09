import json
import subprocess
import sys

import model_regression
from model_regression import compare_model_snapshots


def _snapshot(score=0.6, concept="home", above=True):
    return {
        "libraries": {"torch": "test"},
        "samples": [
            {
                "sample_id": "one",
                "semantic_score": score,
                "concept_match": concept,
                "above_threshold": above,
            }
        ],
    }


def test_model_snapshot_comparison_enforces_score_and_decision_limits(tmp_path):
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps(_snapshot()), encoding="utf-8")
    candidate.write_text(json.dumps(_snapshot(score=0.603)), encoding="utf-8")

    safe = compare_model_snapshots(baseline, candidate)
    changed = compare_model_snapshots(
        baseline,
        candidate,
        max_score_drift=0.001,
    )

    assert safe["safe"]
    assert not changed["safe"]
    assert safe["metrics"]["threshold_decision_agreement"] == 1.0


def test_capture_uses_evaluation_ids_without_copying_text(tmp_path, monkeypatch):
    annotations = tmp_path / "annotations.jsonl"
    annotations.write_text(
        json.dumps({"sample_id": "one", "paragraph": "private evaluation text"})
        + "\n",
        encoding="utf-8",
    )

    class FakeMatcher:
        def __init__(self, **_kwargs):
            pass

        def score_paragraphs(self, paragraphs):
            assert paragraphs == ["private evaluation text"]
            return [(0.75, "home memory")]

    monkeypatch.setattr(model_regression, "SemanticMatcher", FakeMatcher)
    monkeypatch.setattr(
        model_regression,
        "_library_versions",
        lambda: {"torch": "test"},
    )
    output = tmp_path / "snapshot.json"

    result = model_regression.capture_model_snapshot(
        annotations,
        output,
        profile_name="3080",
    )

    assert result["sample_count"] == 1
    assert "private evaluation text" not in output.read_text(encoding="utf-8")
    assert result["samples"][0]["above_threshold"]


def test_compare_command_fails_when_regression_limits_are_exceeded(tmp_path):
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps(_snapshot()), encoding="utf-8")
    candidate.write_text(json.dumps(_snapshot(score=0.7)), encoding="utf-8")

    process = subprocess.run(
        [
            sys.executable,
            "main.py",
            "model-validation",
            "compare",
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert process.returncode == 1
    assert '"safe": false' in process.stdout
