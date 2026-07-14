import json

import pyarrow.dataset as ds

from dedupe import DedupIndex
from evaluation import (
    build_annotation_sample,
    compact_replay_reservoir,
    decision_uncertainty,
    evaluation_report,
)
from matcher import Match
from output import OutputWriter
from parquet_export import export_parquet
from record_identity import content_fingerprint, normalize_url, stable_record_id


def _match(url, text):
    return Match(
        url=url,
        warc_date="2026-01-01",
        text=text,
        matched_keywords=["home"],
        semantic_score=0.9,
        concept_match="memories of home",
        crawl_id="crawl",
        narrative_score=12,
    )


def test_stable_identity_normalizes_text_and_handles_invalid_ports():
    first = stable_record_id("crawl", "source", "HTTPS://EXAMPLE.TEST/a", "date", "My  Home")
    second = stable_record_id("crawl", "source", "https://example.test/a", "date", "my home")
    assert first == second
    assert normalize_url("http://example.test:not-a-port/path")


def test_near_dedupe_finds_same_text_at_another_url(tmp_path):
    paragraph = "I remember the home where my family lived and the garden where we played."
    with DedupIndex(tmp_path / "dedupe.db") as index:
        assert (
            index.check_and_add(
                "first",
                content_fingerprint("https://one.test", paragraph),
                paragraph,
                "near",
            )
            is None
        )
        duplicate = index.check_and_add(
            "second",
            content_fingerprint("https://two.test", paragraph),
            paragraph,
            "near",
        )
    assert duplicate is not None
    assert duplicate.kind == "near"
    assert duplicate.canonical_record_id == "first"


def test_partitioned_parquet_export_is_staged_and_deduplicated(tmp_path):
    output_dir = tmp_path / "output"
    writer = OutputWriter(output_dir)
    paragraph = "I remember my childhood home, my family, and our hometown garden."
    for number in (1, 2):
        source = f"crawl-data/source-{number}.wet.gz"
        transaction = writer.begin_source(source)
        transaction.write_matches(
            [_match(f"https://example.test/{number}", paragraph)],
            [("en", 0.99)],
        )
        transaction.commit()

    target = tmp_path / "parquet"
    manifest = export_parquet(output_dir, target, dedupe="near")

    assert manifest["rows"] == 1
    assert manifest["duplicates"]["exact"] == 1
    assert ds.dataset(target / "stories", format="parquet", partitioning="hive").count_rows() == 1
    assert (
        ds.dataset(target / "provenance", format="parquet", partitioning="hive").count_rows()
        == 2
    )
    saved = json.loads((target / "_manifest.json").read_text(encoding="utf-8"))
    assert saved["files"][0]["sha256"]
    assert saved["tables"]["provenance"]["rows"] == 2
    assert saved["dataset_schema_version"] == 3
    assert saved["tables"]["curated"]["rows"] == 1
    curated = ds.dataset(target / "curated", format="parquet", partitioning="hive")
    curated_row = curated.to_table().to_pylist()[0]
    assert curated_row["content_category"] == "personal_prose"
    assert curated_row["curated_default"]

    replacement = export_parquet(output_dir, target, dedupe="exact")
    assert replacement["rows"] == 1
    assert (
        ds.dataset(target / "stories", format="parquet", partitioning="hive").count_rows()
        == 1
    )


def test_annotation_sample_fills_from_real_output_without_live_rejects(tmp_path):
    output_dir = tmp_path / "output"
    writer = OutputWriter(output_dir)
    for number in range(4):
        source = f"crawl-data/evaluation-{number}.wet.gz"
        transaction = writer.begin_source(source)
        transaction.write_matches(
            [_match(f"https://example.test/eval/{number}", f"A distinct home story {number}.")],
            [("en", 0.99)],
        )
        transaction.commit()

    annotation_path = tmp_path / "annotations.jsonl"
    annotation_path.write_text(
        json.dumps(
            {
                "sample_id": "previously-labeled",
                "language": "fr",
                "paragraph": "A previously reviewed real sample.",
                "predicted_accept": True,
                "label": "positive",
                "notes": "keep this label",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = build_annotation_sample(
        size=4,
        output_dir=output_dir,
        candidate_path=tmp_path / "missing-candidates.jsonl",
        annotation_path=annotation_path,
    )
    assert result["samples"] == 4
    assert result["predicted_positive"] == 4
    rows = [json.loads(line) for line in annotation_path.read_text(encoding="utf-8").splitlines()]
    assert any(row["sample_id"] == "previously-labeled" for row in rows)
    assert all("uncertainty_score" in row for row in rows)


def test_evaluation_report_marks_small_threshold_search_as_exploratory(tmp_path):
    annotation_path = tmp_path / "annotations.jsonl"
    rows = [
        {
            "sample_id": "positive",
            "language": "en",
            "semantic_score": 0.55,
            "narrative_score": 12,
            "predicted_accept": True,
            "label": "positive",
        },
        {
            "sample_id": "negative",
            "language": "en",
            "semantic_score": 0.46,
            "narrative_score": 8,
            "predicted_accept": True,
            "label": "negative",
        },
    ]
    annotation_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    report = evaluation_report(annotation_path, tmp_path / "report.json")
    assert not report["baseline"]["ready"]
    assert report["recommended_thresholds"]["exploratory"]
    assert report["overall"]["precision_ci95"]
    assert report["semantic_calibration"]
    assert report["content_taxonomy"]["predicted"]["unknown"] == 2
    assert decision_uncertainty(0.45, 20) == 1.0


def test_replay_reservoir_is_deterministic_and_cross_machine_ready(tmp_path):
    local = tmp_path / "candidate_samples.jsonl"
    replay = tmp_path / "replay.jsonl.gz"
    rows = [
        {
            "sample_id": f"sample-{index}",
            "language": "en" if index % 2 else "fr",
            "predicted_accept": bool(index % 2),
            "semantic_score": 0.45,
            "narrative_score": 8,
            "paragraph": f"sample text {index}",
        }
        for index in range(8)
    ]
    local.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = compact_replay_reservoir(local, replay, max_samples=6)
    first_bytes = replay.read_bytes()
    assert result["samples"] == 6
    assert result["accepted"] > 0
    assert result["rejected"] > 0
    assert not local.exists()

    second = compact_replay_reservoir(local, replay, max_samples=6)
    assert second["samples"] == 6
    assert replay.read_bytes() == first_bytes
