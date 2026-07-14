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
    assert records[0]["schema_version"] == 2
    assert len(records[0]["record_id"]) == 64
    assert len(records[0]["content_fingerprint"]) == 64
    assert writer.verify_source(source) == []


def test_duplicate_record_in_one_transaction_is_written_once(tmp_path):
    writer = OutputWriter(tmp_path / "output")
    source = "crawl-data/example/duplicate.warc.wet.gz"
    transaction = writer.begin_source(source)
    transaction.write_matches([_match(), _match()], [("en", 0.99), ("en", 0.99)])
    transaction.commit()

    records = _read_records(writer.find_source_outputs(source)[0])
    assert len(records) == 1


def test_zero_match_commit_removes_legacy_partial_output(tmp_path):
    writer = OutputWriter(tmp_path / "output")
    source = "crawl-data/example/legacy.warc.wet.gz"
    legacy = writer.legacy_output_path("en", source)
    legacy.parent.mkdir(parents=True)
    with gzip.open(legacy, "wt", encoding="utf-8") as handle:
        handle.write("{}\n")

    writer.begin_source(source).commit()
    assert writer.find_source_outputs(source) == []
    assert not writer.manifest_path(source).exists()


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


def test_compact_manifest_catalog_supports_overrides_and_tombstones(tmp_path):
    writer = OutputWriter(tmp_path / "output")
    source = "crawl-data/example/catalog.warc.wet.gz"
    first = writer.begin_source(source)
    first.write_matches([_match("first version")], [("en", 0.9)])
    first.commit()

    result = writer.compact_manifest_catalog()
    assert result["manifests"] == 1
    assert writer.catalog_path.exists()
    assert not writer.manifest_path(source).exists()
    assert writer.verify_source(source) == []

    replacement = writer.begin_source(source)
    replacement.write_matches([_match("replacement version")], [("en", 0.9)])
    replacement.commit()
    assert _read_records(writer.find_source_outputs(source)[0])[0]["paragraph"] == (
        "replacement version"
    )
    assert writer.verify_source(source) == []

    writer.begin_source(source).commit()
    assert writer.find_source_outputs(source) == []
    assert writer.get_manifest(source) is None
    compacted = writer.compact_manifest_catalog()
    assert compacted["manifests"] == 0
    assert not writer.catalog_path.exists()
