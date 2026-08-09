import hashlib
import json

import pytest

import evidence_bundle
from evidence_bundle import (
    export_evidence_bundle,
    import_evidence_bundle,
    portable_evidence_status,
    validate_evidence_bundle,
)


def _candidate(profile="3080"):
    samples = [
        {
            "sample_id": "one",
            "semantic_score": 0.75,
            "concept_match": "home",
            "above_threshold": True,
        }
    ]
    return {
        "schema_version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "git_commit": "a" * 40,
        "profile": profile,
        "model": {"name": "model", "revision": "revision", "semantic_threshold": 0.45},
        "libraries": {"torch": "1"},
        "annotation_path": "C:/Users/private/annotations.jsonl",
        "samples": samples,
        "sample_count": 1,
        "sample_digest": hashlib.sha256(
            json.dumps(samples, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def test_portable_evidence_strips_paths_validates_digest_and_imports(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_bundle, "PROFILE_EVIDENCE_DIR", tmp_path / "portable")
    candidate = tmp_path / "candidate.json"
    workload = tmp_path / "workload.json"
    outbox = tmp_path / "outbox.json"
    candidate.write_text(json.dumps(_candidate()), encoding="utf-8")
    workload.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "isolated_real_sources",
                "profile": "3080",
                "trial_outputs_agree": True,
                "trials": [
                    {
                        "workers": 7,
                        "completed": 2,
                        "failed": 0,
                        "files_per_hour": 12.0,
                        "output_digest": "b" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = export_evidence_bundle(
        "3080",
        outbox,
        candidate_path=candidate,
        workload_path=workload,
    )
    raw = outbox.read_text(encoding="utf-8")
    assert result["credentials_included"] is False
    assert "annotation_path" not in raw
    assert "C:/Users/private" not in raw
    assert validate_evidence_bundle(outbox)["profile"] == "3080"

    preview = import_evidence_bundle(outbox)
    assert preview["valid"] and preview["dry_run"]
    assert not (tmp_path / "portable" / "3080.json").exists()
    imported = import_evidence_bundle(outbox, apply=True)
    assert imported["applied"]
    assert portable_evidence_status()["complete_workload_profiles"] == ["3080"]

    tampered = json.loads(outbox.read_text(encoding="utf-8"))
    tampered["profile"] = "4090"
    outbox.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        validate_evidence_bundle(outbox)
