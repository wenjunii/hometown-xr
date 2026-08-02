import gzip
import json

import pytest

import evaluation
from annotation_workbench import _public_row
from evaluation import (
    DecisionSampler,
    annotate,
    annotation_queue,
    compact_replay_reservoir,
    evaluation_campaign,
    evaluation_plan,
    evaluation_report,
    label_annotation,
    multilingual_recall_report,
    undo_annotation,
)
from matcher import MatchDecision
from processor import Paragraph
from record_identity import stable_record_id


class FixedLanguageDetector:
    def detect(self, text):
        assert text
        return "en", 0.99


def _paragraph(number, source="source.wet.gz"):
    return Paragraph(
        url=f"https://example.test/{number}",
        warc_date="2026-01-01",
        text=f"A representative paragraph about place and memory number {number}.",
        crawl_id="crawl",
        source_file=source,
        document_id=f"document-{number}",
        paragraph_index=number,
    )


def test_representative_samples_survive_tuning_cap_and_keep_weights(tmp_path):
    path = tmp_path / "candidates.jsonl"
    sampler = DecisionSampler(
        path,
        sample_rate=1.0,
        max_samples=0,
        replay_path=tmp_path / "missing-replay.gz",
    )
    decisions = [
        MatchDecision(
            paragraph=_paragraph(index),
            matched_keywords=["home"],
            semantic_score=0.8,
            concept_match="home memory",
            narrative_score=12,
            accepted=True,
        )
        for index in range(2)
    ]

    assert sampler.observe(decisions, FixedLanguageDetector()) == 2
    assert (
        sampler.observe_shadow(
            [_paragraph(10, "shadow.wet.gz"), _paragraph(11, "shadow.wet.gz")],
            population_size=10,
            language_detector=FixedLanguageDetector(),
            source_probability=0.2,
        )
        == 2
    )

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert all(row["sample_role"] == "benchmark" for row in rows)
    shadow = [row for row in rows if row["sampling_stratum"] == "keyword_reject"]
    assert len(shadow) == 2
    assert shadow[0]["sampling_probability"] == pytest.approx(0.04)
    assert shadow[0]["sample_weight"] == pytest.approx(25.0)
    assert sampler.tuning_written == 0
    assert sampler.benchmark_written == 4


def test_audit_samples_are_tuning_only_and_zero_rate_disables_sampling(tmp_path):
    decision = MatchDecision(
        paragraph=_paragraph(20),
        matched_keywords=["home"],
        semantic_score=0.8,
        concept_match="home memory",
        narrative_score=12,
        accepted=True,
    )
    path = tmp_path / "audit-candidates.jsonl"
    sampler = DecisionSampler(
        path,
        sample_rate=1.0,
        representative=False,
        replay_path=tmp_path / "missing-replay.gz",
    )

    assert sampler.observe([decision], FixedLanguageDetector()) == 1
    assert (
        sampler.observe_shadow(
            [_paragraph(21, "audit-shadow.wet.gz")],
            population_size=10,
            language_detector=FixedLanguageDetector(),
        )
        == 1
    )
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert all(row["sample_role"] == "tuning" for row in rows)
    assert all(row["sampling_probability"] is None for row in rows)
    assert all(row["sample_weight"] is None for row in rows)

    disabled_path = tmp_path / "disabled.jsonl"
    disabled = DecisionSampler(
        disabled_path,
        sample_rate=0.0,
        replay_path=tmp_path / "missing-replay.gz",
    )
    assert disabled.observe([decision], FixedLanguageDetector()) == 0
    assert not disabled_path.exists()


def test_minority_language_probe_adds_bounded_tuning_evidence(tmp_path, monkeypatch):
    class FrenchDetector:
        def detect(self, text):
            assert text
            return "fr", 0.99

    monkeypatch.setattr(evaluation, "EVALUATION_MINORITY_PROBE_RATE", 1.0)
    monkeypatch.setattr(evaluation, "EVALUATION_MINORITY_LANGUAGE_QUOTA", 1)
    path = tmp_path / "minority.jsonl"
    sampler = DecisionSampler(
        path,
        sample_rate=1e-12,
        max_samples=10,
        replay_path=tmp_path / "missing-replay.gz",
    )
    decisions = [
        MatchDecision(
            paragraph=_paragraph(index + 100),
            matched_keywords=["patrie"],
            semantic_score=0.8,
            concept_match="home memory",
            narrative_score=20,
            accepted=True,
        )
        for index in range(2)
    ]

    assert sampler.observe(decisions, FrenchDetector()) == 1
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["sample_role"] == "tuning"
    assert row["selection_reason"] == "minority_language"
    assert row["sampling_probability"] is None


def test_probability_sampling_upgrades_legacy_replay_but_not_benchmark(tmp_path):
    paragraph = _paragraph(30)
    sample_id = stable_record_id(
        paragraph.crawl_id,
        paragraph.source_file,
        paragraph.url,
        paragraph.warc_date,
        paragraph.text,
    )
    decision = MatchDecision(
        paragraph=paragraph,
        matched_keywords=["home"],
        semantic_score=0.8,
        concept_match="home memory",
        narrative_score=12,
        accepted=True,
    )
    replay = tmp_path / "replay.jsonl.gz"
    with gzip.open(replay, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"sample_id": sample_id, "sample_role": "tuning"}) + "\n")
    path = tmp_path / "candidate.jsonl"
    sampler = DecisionSampler(path, sample_rate=1.0, replay_path=replay)

    assert sampler.observe([decision], FixedLanguageDetector()) == 1
    upgraded = json.loads(path.read_text(encoding="utf-8"))
    assert upgraded["sample_role"] == "benchmark"

    with gzip.open(replay, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"sample_id": sample_id, "sample_role": "benchmark"}) + "\n")
    skipped = DecisionSampler(
        tmp_path / "skipped.jsonl",
        sample_rate=1.0,
        replay_path=replay,
    )
    assert skipped.observe([decision], FixedLanguageDetector()) == 0


def test_replay_compaction_reserves_tuning_without_biasing_benchmark(tmp_path):
    candidate = tmp_path / "candidates.jsonl"
    replay = tmp_path / "replay.jsonl.gz"
    rows = []
    for index in range(8):
        rows.append(
            {
                "sample_id": f"benchmark-{index}",
                "language": "en",
                "paragraph": f"benchmark {index}",
                "predicted_accept": bool(index % 2),
                "sample_role": "benchmark",
                "sampling_probability": 0.1,
            }
        )
        rows.append(
            {
                "sample_id": f"tuning-{index}",
                "language": "fr",
                "paragraph": f"tuning {index}",
                "predicted_accept": bool(index % 2),
                "sample_role": "tuning",
                "semantic_score": 0.45,
                "narrative_score": 8,
            }
        )
    candidate.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    result = compact_replay_reservoir(candidate, replay, max_samples=8)
    with gzip.open(replay, "rt", encoding="utf-8") as handle:
        selected = [json.loads(line) for line in handle if line.strip()]

    assert result["benchmark"] == 6
    assert result["tuning"] == 2
    benchmark = [row for row in selected if row["sample_role"] == "benchmark"]
    assert all(row["sampling_probability"] == pytest.approx(0.075) for row in benchmark)
    assert all(row["sample_weight"] == pytest.approx(13.333333) for row in benchmark)


def test_report_separates_tuning_holdout_and_end_to_end_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "EVALUATION_MIN_BASELINE_LABELS", 4)
    monkeypatch.setattr(evaluation, "EVALUATION_MIN_HOLDOUT_LABELS", 2)
    path = tmp_path / "annotations.jsonl"
    rows = [
        {
            "sample_id": "tuning-positive",
            "label": "positive",
            "predicted_accept": True,
            "semantic_score": 0.8,
            "narrative_score": 12,
            "sample_role": "benchmark",
            "evaluation_split": "tuning",
            "sampling_stratum": "accepted_output",
            "sampling_probability": 0.5,
        },
        {
            "sample_id": "tuning-negative",
            "label": "negative",
            "predicted_accept": False,
            "semantic_score": 0.3,
            "narrative_score": 4,
            "sample_role": "benchmark",
            "evaluation_split": "tuning",
            "sampling_stratum": "filter_reject",
            "sampling_probability": 0.25,
        },
        {
            "sample_id": "holdout-positive",
            "label": "positive",
            "predicted_accept": True,
            "semantic_score": 0.7,
            "narrative_score": 11,
            "sample_role": "benchmark",
            "evaluation_split": "holdout",
            "sampling_stratum": "accepted_output",
            "sampling_probability": 0.5,
        },
        {
            "sample_id": "holdout-negative",
            "label": "negative",
            "predicted_accept": False,
            "semantic_score": 0.2,
            "narrative_score": 2,
            "sample_role": "benchmark",
            "evaluation_split": "holdout",
            "sampling_stratum": "filter_reject",
            "sampling_probability": 0.25,
        },
        {
            "sample_id": "keyword-miss",
            "label": "positive",
            "predicted_accept": False,
            "semantic_score": None,
            "narrative_score": None,
            "sample_role": "benchmark",
            "evaluation_split": "holdout",
            "sampling_stratum": "keyword_reject",
            "sampling_probability": 0.04,
        },
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    report = evaluation_report(path, tmp_path / "report.json")

    assert report["baseline"]["ready"]
    assert report["holdout"]["ready"]
    assert report["holdout"]["samples"] == 3
    assert report["holdout"]["recommended_threshold_validation"] is not None
    assert report["metric_scope"] == "end_to_end_weighted"
    assert report["weighted_benchmark"]["represented_population"] == pytest.approx(37.0)
    assert report["recommended_thresholds"]["validated_on_holdout"]


def test_annotation_records_provenance_and_supports_undo(tmp_path):
    path = tmp_path / "annotations.jsonl"
    path.write_text(
        json.dumps(
            {
                "sample_id": "sample-one",
                "language": "en",
                "paragraph": "I remember my family home.",
                "predicted_accept": True,
                "evaluation_split": "tuning",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    answers = iter(["p"])
    result = annotate(
        path,
        limit=1,
        annotator="reviewer",
        quick=True,
        input_func=lambda prompt: next(answers),
    )
    labeled = json.loads(path.read_text(encoding="utf-8"))
    assert result["labeled_now"] == 1
    assert labeled["label"] == "positive"
    assert labeled["annotator"] == "reviewer"
    assert labeled["labeled_at"]

    undone = undo_annotation(path, "sample-one")
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert undone["restored_label"] is None
    assert "label" not in restored
    assert "annotator" not in restored


def test_workbench_label_api_preserves_history_and_balances_queue(tmp_path):
    path = tmp_path / "annotations.jsonl"
    rows = [
        {
            "sample_id": "fr-one",
            "language": "fr",
            "paragraph": "Je me souviens de ma ville natale.",
            "predicted_accept": False,
            "evaluation_split": "tuning",
        },
        {
            "sample_id": "en-one",
            "language": "en",
            "paragraph": "I remember my hometown.",
            "predicted_accept": True,
            "evaluation_split": "holdout",
            "sample_role": "benchmark",
        },
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    queue = annotation_queue(path)
    assert {row["language"] for row in queue} == {"en", "fr"}
    result = label_annotation(
        "fr-one",
        "positive",
        content_label="personal_prose",
        notes="clear memory",
        annotator="reviewer",
        annotation_path=path,
    )
    assert result["labeled_total"] == 1
    labeled = [
        row
        for row in map(json.loads, path.read_text(encoding="utf-8").splitlines())
        if row["sample_id"] == "fr-one"
    ][0]
    assert labeled["label_history"][0]["label"] is None
    assert labeled["notes"] == "clear memory"


def test_multilingual_report_finds_anchor_gaps_and_keyword_misses(tmp_path):
    annotations = tmp_path / "annotations.jsonl"
    annotations.write_text(
        json.dumps(
            {
                "sample_id": "miss",
                "language": "en",
                "paragraph": "A true story the keyword filter missed.",
                "predicted_accept": False,
                "rejection_reason": "keyword_prefilter",
                "label": "positive",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = multilingual_recall_report(
        annotations,
        tmp_path / "candidates.jsonl",
        tmp_path / "replay.jsonl.gz",
        tmp_path / "report.json",
    )
    assert report["languages_missing_native_anchor"] == []
    assert report["labeled_keyword_misses"] == 1
    assert report["languages"]["en"]["keyword_miss_positives"] == 1


def test_workbench_keeps_holdout_model_evidence_blind():
    row = _public_row(
        {
            "sample_id": "holdout",
            "sample_role": "benchmark",
            "evaluation_split": "holdout",
            "predicted_accept": True,
            "semantic_score": 0.9,
            "narrative_score": 12,
            "predicted_content_category": "personal_prose",
        }
    )
    assert row["blind"]
    assert "predicted_accept" not in row
    assert "semantic_score" not in row
    assert "predicted_content_category" not in row


def test_evaluation_plan_balances_human_work_without_labeling(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "EVALUATION_MIN_BASELINE_LABELS", 4)
    monkeypatch.setattr(evaluation, "EVALUATION_MIN_HOLDOUT_LABELS", 2)
    path = tmp_path / "annotations.jsonl"
    rows = [
        {
            "sample_id": f"sample-{index}",
            "paragraph": f"paragraph {index}",
            "predicted_accept": bool(index % 2),
            "evaluation_split": "holdout" if index < 2 else "tuning",
            "sample_role": "benchmark",
        }
        for index in range(4)
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    result = evaluation_plan(path)

    assert result["remaining_to_target"] == 4
    assert sum(step["planned"] for step in result["steps"]) == 4
    assert result["requires_human_judgment"]
    assert all("evaluation annotate" in step["command"] for step in result["steps"])
    assert all("label" not in row for row in map(json.loads, path.read_text().splitlines()))


def test_evaluation_campaign_is_resumable_balanced_and_reports_blocked_phases(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(evaluation, "EVALUATION_MIN_BASELINE_LABELS", 4)
    monkeypatch.setattr(evaluation, "EVALUATION_MIN_HOLDOUT_LABELS", 2)
    path = tmp_path / "annotations.jsonl"
    rows = [
        {
            "sample_id": "holdout-accepted",
            "paragraph": "accepted holdout",
            "predicted_accept": True,
            "evaluation_split": "holdout",
            "sample_role": "benchmark",
            "language": "en",
            "label": "positive",
        },
        {
            "sample_id": "tuning-rejected",
            "paragraph": "rejected tuning",
            "predicted_accept": False,
            "evaluation_split": "tuning",
            "language": "fr",
        },
        {
            "sample_id": "tuning-accepted",
            "paragraph": "accepted tuning",
            "predicted_accept": True,
            "evaluation_split": "tuning",
            "language": "en",
        },
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    campaign = evaluation_campaign(path)

    assert campaign["target"] == 4
    assert campaign["completed"] == 1
    assert campaign["queued"] == 2
    assert campaign["blocked_phases"][0]["id"] == "holdout-rejected"
    assert {row["campaign_phase"] for row in campaign["samples"]} == {
        "tuning-accepted",
        "tuning-rejected",
    }
    assert all("label" not in row for row in rows[1:])
