import json
from pathlib import Path

import pytest

from audit import (
    adopt_audit_evidence,
    archive_adoption_evidence,
    audit_evidence_status,
    build_audit_plan,
    compare_audit_outputs,
    load_adoption_evidence,
    output_match_set_digest,
)
from matcher import Match
from output import OutputWriter
from progress import ProgressTracker


def _write(writer, source, text, raw_text=""):
    transaction = writer.begin_source(source)
    transaction.write_matches(
        [
            Match(
                url="https://example.test/story",
                warc_date="2026-01-01",
                text=text,
                raw_text=raw_text,
                matched_keywords=["home"],
                semantic_score=0.8,
                concept_match="home memory",
                crawl_id="crawl",
            )
        ],
        [("en", 0.99)],
    )
    transaction.commit()


def test_audit_plan_and_comparison_preserve_historical_output(tmp_path):
    tracker = ProgressTracker(tmp_path / "historical.db")
    source = "crawl-data/source.wet.gz"
    tracker.initialize_paths([source], "crawl")
    tracker.mark_completed(source, 10, 1, filter_signature="old")
    plan = build_audit_plan("new", 1, tracker=tracker)
    assert plan["total_sources"] == 1
    assert plan["preserves_historical_state"]

    historical = OutputWriter(tmp_path / "historical-output")
    audit = OutputWriter(tmp_path / "audit-output")
    damaged = "I remember my family\u00e2\u20ac\u2122s home &amp; garden."
    repaired = "I remember my family\u2019s home & garden."
    _write(historical, source, damaged)
    _write(audit, source, repaired, raw_text=damaged)

    result = compare_audit_outputs(
        plan,
        {source: {"status": "completed", "error": None}},
        historical_output=historical.output_dir,
        audit_output=audit.output_dir,
    )
    assert result["summary"]["equivalent_normalized_match_sets"]
    assert result["summary"]["normalized_text_repairs"] == 1
    assert result["summary"]["historical_state_changed"] is False


def test_audit_report_is_validated_before_signature_adoption(tmp_path):
    report_path = tmp_path / "report.json"
    report = {
        "audit_id": "audit-one",
        "filter_signature": "new",
        "summary": {"historical_state_changed": False},
        "adoption": {
            "eligible_crawls": ["crawl"],
            "minimum_sources_per_crawl": 1,
            "by_crawl": {
                "crawl": {
                    "eligible": True,
                    "selected_sources": 1,
                    "completed_sources": 1,
                }
            },
        },
        "sources": [
            {
                "crawl_id": "crawl",
                "audit_status": "completed",
                "added_matches": 0,
                "removed_matches": 0,
            }
        ],
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")

    evidence = load_adoption_evidence(report_path, "new", ["crawl"])
    assert evidence["audit_id"] == "audit-one"
    assert evidence["eligible_crawls"] == ["crawl"]
    assert len(evidence["report_sha256"]) == 64
    archived = archive_adoption_evidence(
        report_path,
        evidence,
        target_dir=tmp_path / "shared-evidence",
    )
    assert archived.read_bytes() == report_path.read_bytes()
    assert archive_adoption_evidence(
        report_path,
        evidence,
        target_dir=tmp_path / "shared-evidence",
    ) == archived
    status = audit_evidence_status(report_path, "new", ["crawl"])
    assert status["valid"]
    assert status["eligible_crawls"] == ["crawl"]
    assert "audit adopt" in status["adopt_command"]

    report["sources"][0]["added_matches"] = 1
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="internally inconsistent"):
        load_adoption_evidence(report_path, "new", ["crawl"])


def test_audit_adoption_archives_evidence_and_stamps_only_validated_crawl(tmp_path):
    tracker = ProgressTracker(tmp_path / "progress.db")
    tracker.initialize_paths(["one.wet.gz", "two.wet.gz"], "crawl")
    tracker.mark_completed("one.wet.gz", 10, 1)
    tracker.mark_completed("two.wet.gz", 10, 0)
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "audit_id": "audit-adopt",
                "filter_signature": "current",
                "summary": {"historical_state_changed": False},
                "adoption": {
                    "eligible_crawls": ["crawl"],
                    "minimum_sources_per_crawl": 1,
                    "by_crawl": {
                        "crawl": {
                            "eligible": True,
                            "selected_sources": 1,
                            "completed_sources": 1,
                        }
                    },
                },
                "sources": [
                    {
                        "crawl_id": "crawl",
                        "audit_status": "completed",
                        "added_matches": 0,
                        "removed_matches": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = adopt_audit_evidence(
        report_path,
        "current",
        tracker=tracker,
        target_dir=tmp_path / "evidence",
    )

    assert result["adopted"]
    assert result["rows_stamped"] == 2
    assert Path(result["archived_report"]).exists()
    assert tracker.get_filter_signature_summary("current")["current"] == 2


def test_output_match_set_digest_ignores_normalization_repairs(tmp_path):
    first = OutputWriter(tmp_path / "first")
    second = OutputWriter(tmp_path / "second")
    source = "crawl-data/source.wet.gz"
    damaged = "I remember my family\u00e2\u20ac\u2122s home &amp; garden."
    repaired = "I remember my family\u2019s home & garden."
    _write(first, source, damaged)
    _write(second, source, repaired, raw_text=damaged)

    assert output_match_set_digest(first.output_dir, [source]) == output_match_set_digest(
        second.output_dir,
        [source],
    )
