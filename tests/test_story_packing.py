import gzip
import json

import pytest

from story_packing import (
    build_story_packs,
    restore_story_packs,
    story_pack_status,
    verify_story_packs,
)


def _fragment(path, source, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "source_file": source,
                    "record_id": record,
                    "story": {"text": "source story"},
                }
            )
            + "\n"
        )


def test_story_packs_are_byte_stable_and_restore_exact_fragments(tmp_path):
    stories = tmp_path / "stories"
    first = stories / "_records" / f"{'0' * 20}.jsonl.gz"
    second = stories / "_records" / f"{'f' * 20}.jsonl.gz"
    _fragment(first, "crawl-data/one", "one")
    _fragment(second, "crawl-data/two", "two")
    expected = {first.name: first.read_bytes(), second.name: second.read_bytes()}

    built = build_story_packs(stories)
    first_pack_bytes = {
        path.name: path.read_bytes()
        for path in (stories / "_packs").glob("*.gz")
    }
    build_story_packs(stories)

    assert built["fragments"] == 2
    assert verify_story_packs(stories)["valid"]
    assert {
        path.name: path.read_bytes()
        for path in (stories / "_packs").glob("*.gz")
    } == first_pack_bytes

    first.unlink()
    second.unlink()
    restored = restore_story_packs(stories)

    assert restored["restored_fragments"] == 2
    assert first.read_bytes() == expected[first.name]
    assert second.read_bytes() == expected[second.name]
    assert story_pack_status(stories)["safe_to_pull"]


def test_story_pack_restore_refuses_changed_local_fragment(tmp_path):
    stories = tmp_path / "stories"
    fragment = stories / "_records" / f"{'a' * 20}.jsonl.gz"
    _fragment(fragment, "crawl-data/one", "one")
    build_story_packs(stories)
    fragment.write_bytes(b"local uncheckpointed work")

    status = story_pack_status(stories)

    assert not status["safe_to_pull"]
    assert status["changed_fragments"]
    with pytest.raises(RuntimeError, match="differ from the checkpoint"):
        restore_story_packs(stories)
    restored = restore_story_packs(stories, replace=True)
    assert restored["replaced_fragments"] == 1


def test_story_pack_verification_detects_pack_tampering(tmp_path):
    stories = tmp_path / "stories"
    fragment = stories / "_records" / f"{'b' * 20}.jsonl.gz"
    _fragment(fragment, "crawl-data/one", "one")
    build_story_packs(stories)
    pack = next((stories / "_packs").glob("pack-*.jsonl.gz"))
    pack.write_bytes(pack.read_bytes() + b"tampered")

    verification = verify_story_packs(stories)

    assert not verification["valid"]
    assert verification["errors"]


def test_story_pack_verification_reports_malformed_metadata(tmp_path):
    stories = tmp_path / "stories"
    fragment = stories / "_records" / f"{'d' * 20}.jsonl.gz"
    _fragment(fragment, "crawl-data/one", "one")
    build_story_packs(stories)
    pack = next((stories / "_packs").glob("pack-*.jsonl.gz"))
    with gzip.open(pack, "rt", encoding="utf-8") as handle:
        row = json.loads(next(handle))
    row["bytes"] = "not-a-number"
    with gzip.open(pack, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")

    verification = verify_story_packs(stories)

    assert not verification["valid"]
    assert any("byte count" in error for error in verification["errors"])
