import audit
import benchmark
from benchmark import run_workload_benchmark


def test_real_workload_benchmark_uses_repeatable_outputs_for_recommendation(
    tmp_path,
    monkeypatch,
):
    plan = {
        "total_sources": 2,
        "sources": [
            {"file_path": "one.wet.gz", "crawl_id": "crawl"},
            {"file_path": "two.wet.gz", "crawl_id": "crawl"},
        ],
    }
    monkeypatch.setattr(audit, "build_audit_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(benchmark, "LOCAL_EVIDENCE_DIR", tmp_path / "evidence")

    def fake_run(plan, settings, audit_dir, sample_rate):
        del plan, audit_dir, sample_rate
        return {
            "audit_id": settings.run_id,
            "audit_root": str(tmp_path / settings.run_id),
            "metrics": {
                "files_completed": 2,
                "files_failed": 0,
                "elapsed_seconds": 10,
                "rates": {
                    "files_per_hour": settings.workers * 100,
                    "megabytes_per_second": settings.workers,
                },
                "resources": {
                    "peak_worker_rss_mb": settings.workers * 100,
                    "peak_vram_mb": 1000,
                },
                "failure_categories": {},
                "process_pool_restarts": 0,
            },
            "summary": {
                "audit_matches": 3,
                "equivalent_normalized_match_sets": True,
            },
        }

    monkeypatch.setattr(audit, "run_audit", fake_run)
    monkeypatch.setattr(audit, "output_match_set_digest", lambda *args: "same")

    result = run_workload_benchmark(
        "3080",
        "crawl",
        source_count=2,
        worker_counts=[1, 3],
        write=False,
        audit_dir=tmp_path,
    )

    assert result["trial_outputs_agree"]
    assert result["recommended_workers"] == 3
    assert result["recommendation_applied"] is None


def test_real_workload_benchmark_refuses_partial_trial_success(tmp_path, monkeypatch):
    plan = {
        "total_sources": 1,
        "sources": [{"file_path": "one.wet.gz", "crawl_id": "crawl"}],
    }
    monkeypatch.setattr(audit, "build_audit_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(benchmark, "LOCAL_EVIDENCE_DIR", tmp_path / "evidence")

    def fake_run(plan, settings, audit_dir, sample_rate):
        del plan, audit_dir, sample_rate
        failed = settings.workers == 3
        return {
            "audit_id": settings.run_id,
            "audit_root": str(tmp_path / settings.run_id),
            "metrics": {
                "files_completed": 0 if failed else 1,
                "files_failed": 1 if failed else 0,
                "elapsed_seconds": 10,
                "rates": {"files_per_hour": settings.workers * 100},
                "resources": {},
            },
            "summary": {
                "audit_matches": 1,
                "equivalent_normalized_match_sets": not failed,
            },
        }

    monkeypatch.setattr(audit, "run_audit", fake_run)
    monkeypatch.setattr(audit, "output_match_set_digest", lambda *args: "same")

    result = run_workload_benchmark(
        "3080",
        "crawl",
        source_count=1,
        worker_counts=[1, 3],
        write=False,
        audit_dir=tmp_path,
    )

    assert not result["trial_outputs_agree"]
    assert result["recommended_workers"] is None
    assert result["warning"]
