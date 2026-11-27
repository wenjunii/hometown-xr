import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gpu_evidence import REQUIRED_PROFILES, compare_gpu_evidence
from model_migration import approve_model_migration, build_model_migration_plan
from signatures import build_filter_signature

PIN = "a" * 40
MODEL_LIBRARIES = {
    "torch": "2.13.0+cu130",
    "sentence-transformers": "5.6.1",
    "transformers": "5.10.4",
}


def _digest(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _snapshot(profile="3080", libraries=None):
    return {
        "schema_version": 1,
        "profile": profile,
        "git_commit": PIN,
        "libraries": libraries or MODEL_LIBRARIES,
        "model": {"name": "model", "revision": "revision", "semantic_threshold": 0.45},
        "runtime": {"device": "cuda", "precision": "fp16"},
        "sample_count": 400,
        "samples": [
            {
                "sample_id": f"sample-{index}",
                "semantic_score": 0.7,
                "concept_match": "home",
                "above_threshold": True,
            }
            for index in range(400)
        ],
    }


def _bundle(profile):
    signature = build_filter_signature()
    return {
        "schema_version": 1,
        "profile": profile,
        "git_commit": PIN,
        "filter_signature": signature,
        "generated_text": False,
        "contains_credentials": False,
        "contains_source_text": False,
        "model_snapshot": _snapshot(profile),
        "workload_benchmark": {
            "profile": profile,
            "git_commit": PIN,
            "filter_signature": signature,
            "mode": "isolated_real_sources",
            "crawl_id": "CC-MAIN-2014-15",
            "sources_per_trial": 5,
            "source_files": [f"source-{i}.warc.wet.gz" for i in range(5)],
            "runtime": {"precision": "fp16"},
            "cache_enabled": False,
            "historical_state_changed": False,
            "trial_outputs_agree": True,
            "trials": [
                {
                    "workers": 7,
                    "completed": 5,
                    "failed": 0,
                    "process_pool_restarts": 0,
                    "equivalent_output": True,
                    "audit_matches": 2,
                    "output_digest": "b" * 64,
                }
            ],
        },
    }


def _write_bundle(path, bundle):
    snapshot = bundle["model_snapshot"]
    snapshot["sample_digest"] = _digest(snapshot["samples"])
    bundle.pop("content_sha256", None)
    bundle["content_sha256"] = _digest(bundle)
    path.write_text(json.dumps(bundle), encoding="utf-8")


@pytest.fixture
def ready_inputs(tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(_snapshot()), encoding="utf-8")
    candidates = {}
    evidence_paths = {}
    for profile in REQUIRED_PROFILES:
        path = tmp_path / f"candidate-{profile}.json"
        path.write_text(json.dumps(_snapshot(profile)), encoding="utf-8")
        candidates[profile] = path
        evidence_paths[profile] = tmp_path / f"bundle-{profile}.json"
        _write_bundle(evidence_paths[profile], _bundle(profile))
    return {
        "baseline_path": baseline,
        "candidate_paths": candidates,
        "expected_commit": PIN,
        "evidence_paths": evidence_paths,
        "evaluation": {
            "labeled": 100,
            "baseline": {"ready": True, "holdout_ready": True, "holdout_labels": 20},
        },
        "filters": {"completed": 100, "current": 100, "unknown": 0, "stale": 0},
        "dependencies": {"valid": True},
    }


def _compare(inputs):
    return compare_gpu_evidence(
        inputs["expected_commit"], inputs["evidence_paths"], baseline_path=inputs["baseline_path"]
    )


def test_model_migration_gate_requires_every_evidence_class(tmp_path, ready_inputs):
    inputs = ready_inputs
    inputs["evaluation"] = {
        "labeled": 0,
        "baseline": {"ready": False, "holdout_ready": False, "holdout_labels": 0},
    }
    inputs["filters"] = {"completed": 100, "unknown": 1, "stale": 0}
    inputs["evidence_paths"]["4090"].unlink()

    result = build_model_migration_plan(**inputs)

    assert not result["ready"]
    blockers = {item["name"] for item in result["blockers"]}
    assert "human_evaluation" in blockers
    assert "real_workload_benchmarks" in blockers
    assert "historical_filter_evidence" in blockers
    assert result["comparisons"]["4090"]["safe"]  # Raw model comparisons are only diagnostic.
    assert not result["gpu_evidence"]["profiles"]["4090"]["valid"]
    assert result["gpu_evidence"]["profiles"]["5090"]["valid"]
    with pytest.raises(ValueError, match="blocked"):
        approve_model_migration(result, tmp_path / "evidence.json")


def test_model_migration_approval_records_only_fully_validated_plan(tmp_path, ready_inputs):
    result = build_model_migration_plan(**ready_inputs)

    evidence = approve_model_migration(result, tmp_path / "evidence.json")

    assert result["ready"]
    assert result["status"] == "ready_for_lock_update"
    assert all(item["safe"] for item in result["comparisons"].values())
    assert evidence["approved"]
    payload = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert payload["approved"]
    assert payload["blockers"] == []


def test_gpu_compare_allows_observed_cross_pc_drift_and_is_read_only(tmp_path, ready_inputs):
    candidate = _bundle("5090")
    candidate["model_snapshot"]["samples"][0]["semantic_score"] -= 0.00097656
    candidate["model_snapshot"]["samples"][0]["concept_match"] = "heritage"
    candidate["workload_benchmark"]["source_files"].reverse()
    _write_bundle(ready_inputs["evidence_paths"]["5090"], candidate)
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}

    result = _compare(ready_inputs)

    assert result["valid"] and result["read_only"]
    comparison = result["profiles"]["5090"]["reference_comparison"]
    assert comparison["samples"]["shared"] == 400
    assert comparison["metrics"]["threshold_decision_agreement"] == 1
    assert comparison["metrics"]["concept_agreement"] == 0.9975
    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("pin", [None, "main", "a" * 8, "d" * 40])
def test_gpu_compare_needs_the_exact_prepared_commit(ready_inputs, pin):
    ready_inputs["expected_commit"] = pin
    assert not _compare(ready_inputs)["valid"]
    assert not build_model_migration_plan(**ready_inputs)["ready"]


@pytest.mark.parametrize(
    ("key_path", "value", "error"),
    [
        (("workload_benchmark", "git_commit"), "e" * 40, "expected runtime commit"),
        (("workload_benchmark", "filter_signature"), "e" * 64, "filter signatures do not match"),
        (("model_snapshot", "runtime", "device"), "cpu", "CUDA FP16"),
        (("model_snapshot", "runtime", "precision"), "fp32", "CUDA FP16"),
        (("workload_benchmark", "runtime", "precision"), "fp32", "benchmark must use FP16"),
        (("workload_benchmark", "cache_enabled"), True, "no cache"),
        (("workload_benchmark", "historical_state_changed"), True, "historical state unchanged"),
        (("workload_benchmark", "trials"), [], "successful 7-worker trial"),
        (("workload_benchmark", "trials", 0, "completed"), 4, "successful 7-worker trial"),
        (("workload_benchmark", "trials", 0, "failed"), 1, "successful 7-worker trial"),
        (("workload_benchmark", "trials", 0, "workers"), 8, "successful 7-worker trial"),
        (("workload_benchmark", "trials", 0, "process_pool_restarts"), 1, "no failures or restarts"),
        (("workload_benchmark", "trials", 0, "equivalent_output"), False, "historical matches"),
        (("workload_benchmark", "trials", 0, "output_digest"), "e" * 64, "output differs"),
        (("workload_benchmark", "source_files", 0), "other.wet.gz", "sources differ"),
        (("workload_benchmark", "source_files", 0), "source-1.warc.wet.gz", "different sources"),
        (("model_snapshot", "libraries", "torch"), "different", "library versions differ"),
        (("model_snapshot", "samples", 0, "above_threshold"), False, "exceeds existing limits"),
        (("model_snapshot", "samples", 0, "semantic_score"), float("nan"), "valid samples"),
        (("model_snapshot", "samples", 0, "sample_id"), "sample-1", "distinct"),
        (("contains_credentials",), True, "sanitized"),
    ],
)
def test_gpu_compare_rejects_bad_workloads_and_snapshots(ready_inputs, key_path, value, error):
    candidate = _bundle("5090")
    updated = candidate
    for key in key_path[:-1]:
        updated = updated[key]
    updated[key_path[-1]] = value
    _write_bundle(ready_inputs["evidence_paths"]["5090"], candidate)

    result = _compare(ready_inputs)

    assert not result["valid"]
    assert any(error in message for message in result["profiles"]["5090"]["errors"])


def test_gpu_compare_reports_non_overlapping_samples(ready_inputs):
    candidate = _bundle("5090")
    for sample in candidate["model_snapshot"]["samples"]:
        sample["sample_id"] = "unrelated-" + sample["sample_id"]
    _write_bundle(ready_inputs["evidence_paths"]["5090"], candidate)

    result = _compare(ready_inputs)

    assert not result["valid"]
    assert result["profiles"]["5090"]["reference_comparison"]["samples"]["shared"] == 0


def test_gpu_compare_reports_corruption_and_keeps_other_profiles(ready_inputs):
    path = ready_inputs["evidence_paths"]["4090"]
    bundle = json.loads(path.read_text(encoding="utf-8"))
    bundle["git_commit"] = "z" * 40
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = _compare(ready_inputs)

    assert not result["valid"]
    assert "digest" in result["profiles"]["4090"]["errors"][0]
    assert result["profiles"]["3080"]["valid"]
    assert result["profiles"]["5090"]["valid"]


def test_migration_rejects_agreeing_bundles_from_a_different_filter(ready_inputs):
    for profile, path in ready_inputs["evidence_paths"].items():
        bundle = _bundle(profile)
        bundle["filter_signature"] = bundle["workload_benchmark"]["filter_signature"] = "e" * 64
        _write_bundle(path, bundle)
    assert _compare(ready_inputs)["valid"]

    result = build_model_migration_plan(**ready_inputs)

    assert not result["ready"]
    assert any("checkout's filter signature" in row["detail"] for row in result["blockers"])


@pytest.mark.parametrize("use_wrapper", [False, True])
def test_evidence_compare_cli_exits_nonzero_with_missing_4090(ready_inputs, use_wrapper):
    ready_inputs["evidence_paths"]["4090"].unlink()
    root = Path(__file__).resolve().parents[1]
    if use_wrapper:
        pwsh = shutil.which("pwsh")
        if not pwsh:
            pytest.skip("PowerShell is not installed")
        args = [
            pwsh, "-NoProfile", "-File", str(root / "scripts" / "evidence.ps1"),
            "-Action", "compare", "-ExpectedCommit", PIN,
            "-Baseline", str(ready_inputs["baseline_path"]),
            "-PythonExecutable", sys.executable,
        ]
        evidence_arg = "-Evidence"
    else:
        args = [
            sys.executable, "-B", str(root / "main.py"), "evidence", "compare",
            "--expected-commit", PIN, "--baseline", str(ready_inputs["baseline_path"]),
        ]
        evidence_arg = "--evidence-"
    for profile, path in ready_inputs["evidence_paths"].items():
        args.extend((f"{evidence_arg}{profile}", str(path)))

    run = subprocess.run(args, capture_output=True, text=True, timeout=30)

    assert run.returncode == 1, run.stderr
    result = json.loads(run.stdout)
    assert not result["profiles"]["4090"]["present"]
    assert result["profiles"]["5090"]["valid"]
