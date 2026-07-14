import gzip
import json

import pytest

import output
from matcher import Match
from output import OutputWriter


def _match(text="I remember my childhood home and my family."):
    return Match(
        url="https://example.test/story",
        warc_date="2026-01-01",
        text=text,
        matched_keywords=["home"],
        semantic_score=0.9,
        concept_match="memories of home",
        crawl_id="crawl",
    )


def _read_records(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_source_commit_is_idempotent_and_records_source(tmp_path):
    writer = OutputWriter(tmp_path / "output")
    source = "crawl-data/example/source.warc.wet.gz"

    for _ in range(2):
        transaction = writer.begin_source(source)
        transaction.write_matches([_match()], [("en", 0.99)])
        transaction.commit()

    outputs = writer.find_source_outputs(source)
    assert len(outputs) == 1
    records = _read_records(outputs[0])
    assert len(records) == 1
    assert records[0]["source_file"] == source


def test_zero_match_commit_removes_legacy_partial_output(tmp_path):
    writer = OutputWriter(tmp_path / "output")
    source = "crawl-data/example/legacy.warc.wet.gz"
    legacy = writer.legacy_output_path("en", source)
    legacy.parent.mkdir(parents=True)
    with gzip.open(legacy, "wt", encoding="utf-8") as handle:
        handle.write("{}\n")

    writer.begin_source(source).commit()
    assert writer.find_source_outputs(source) == []


def test_commit_rolls_back_existing_output_when_install_fails(tmp_path, monkeypatch):
    writer = OutputWriter(tmp_path / "output")
    source = "crawl-data/example/rollback.warc.wet.gz"
    first = writer.begin_source(source)
    first.write_matches([_match("old")], [("en", 0.9)])
    first.commit()

    second = writer.begin_source(source)
    second.write_matches([_match("new")], [("en", 0.9)])
    real_replace = output.os.replace
    failed = False

    def fail_stage_install(src, dst):
        nonlocal failed
        src_path = output.Path(src)
        if src_path.parent == second.staging_dir and not failed:
            failed = True
            raise OSError("simulated install failure")
        return real_replace(src, dst)

    monkeypatch.setattr(output.os, "replace", fail_stage_install)
    with pytest.raises(OSError, match="simulated"):
        second.commit()
    second.abort()

    records = _read_records(writer.find_source_outputs(source)[0])
    assert records[0]["paragraph"] == "old"
