import gzip
import json

from metrics import MetricsRecorder, compact_run_history


def test_run_provenance_compacts_into_deterministic_shared_history(tmp_path):
    metrics_dir = tmp_path / "metrics"
    target = tmp_path / "run-history.jsonl.gz"
    recorder = MetricsRecorder(
        "3080",
        7,
        800,
        metrics_dir,
        "RTX 3080",
        provenance={"run_id": "run-one", "filter_signature": "abc"},
    )
    recorder.close()

    result = compact_run_history(metrics_dir, target)
    first_bytes = target.read_bytes()
    with gzip.open(target, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    assert result["runs"] == 1
    assert rows[0]["session_id"] == "run-one"
    assert rows[0]["provenance"]["filter_signature"] == "abc"

    compact_run_history(metrics_dir, target)
    assert target.read_bytes() == first_bytes
