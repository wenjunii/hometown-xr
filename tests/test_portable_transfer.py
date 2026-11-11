import gzip
import json
import sqlite3
import stat
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

import portable_transfer as transfer
from workstation_guard import WorkstationGuardError, WorkstationLease, get_workstation_lease


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "source"
    (root / "data/output").mkdir(parents=True)
    (root / "main.py").write_text('print("example")\n', encoding="utf-8")
    (root / "data/output/en.jsonl").write_text('{"text":"My childhood home."}\n', encoding="utf-8")
    with sqlite3.connect(root / "data/progress.db") as db:
        db.execute("CREATE TABLE processing_state (source TEXT, status TEXT)")
        db.execute("INSERT INTO processing_state VALUES ('source.wet.gz', 'completed')")
    return root


@pytest.fixture
def published(project, tmp_path):
    store = tmp_path / "store"
    result = transfer.export_snapshot(project, store, apply=True)
    return store, result


def rewrite(path, change):
    with zipfile.ZipFile(path) as source:
        entries = [(info, source.read(info.filename)) for info in source.infolist()]
    with zipfile.ZipFile(path, "w") as output:
        for info, content in entries:
            change(output, info, content)


def test_dry_run_does_not_create_store_or_change_project(project, tmp_path):
    before = transfer.inventory(project)
    store = tmp_path / "store"
    result = transfer.export_snapshot(project, store)
    assert result["applied"] is False
    assert not store.exists()
    assert not (project / transfer.CONFIG).exists()
    assert transfer.inventory(project) == before


def test_roundtrip_preserves_source_and_labels_but_excludes_machine_state(project, tmp_path):
    (project / "data/evaluation").mkdir()
    labels = b'{"label":"accepted","notes":"human judgment"}\n'
    (project / "data/evaluation/annotations.jsonl").write_bytes(labels)
    for name in (
        ".env",
        "data/hardware-profile.local.json",
        "data/models/model.bin",
        ".venv/file.txt",
    ):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("local only", encoding="utf-8")
    (project / "data/hardware-profile.local.json").write_text('{"profile":"3080","workers":7}')
    store = tmp_path / "store"
    result = transfer.export_snapshot(project, store, apply=True)
    target = tmp_path / "received"
    plan = transfer.import_snapshot(project, store, target)
    assert plan["valid"] and not plan["applied"] and not target.exists()
    imported = transfer.import_snapshot(project, store, target, apply=True)
    assert imported["applied"]
    assert (target / "main.py").read_bytes() == (project / "main.py").read_bytes()
    assert (target / "data/evaluation/annotations.jsonl").read_bytes() == labels
    assert not (target / ".env").exists()
    assert not (target / "data/models/model.bin").exists()
    assert not (target / "data/hardware-profile.local.json").exists()
    assert transfer.verify_snapshot(result["path"])["valid"]
    assert get_workstation_lease("4090", root=target).preflight()["ready"]
    with gzip.open(target / "data/checkpoints/progress.db.gz", "rb") as compressed:
        assert compressed.read() == (target / "data/progress.db").read_bytes()


def test_shipped_receiver_runs_without_gpu_dependencies(project, published, tmp_path):
    import subprocess
    import sys

    store, result = published
    tool = store / "tools" / result["snapshot_id"] / "portable_transfer.py"
    assert tool.exists()
    target = tmp_path / "bootstrap-restored"
    run = subprocess.run(
        [
            sys.executable,
            "-S",
            str(tool),
            "transfer",
            "import",
            "--store",
            str(store),
            "--destination",
            str(target),
            "--yes",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["applied"]
    assert (target / transfer.CONFIG).exists()


def test_shipped_receiver_can_inspect_its_parent_store(published, tmp_path):
    import subprocess
    import sys

    store, result = published
    tool = store / "tools" / result["snapshot_id"] / "portable_transfer.py"
    run = subprocess.run(
        [sys.executable, "-S", str(tool), "transfer", "status", "--store", str(store)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["snapshot_id"] == result["snapshot_id"]


@pytest.mark.parametrize(
    "action, arguments, message",
    [
        ("status", ["-Destination", "unused"], "Destination is valid only with import"),
        ("export", ["-Path", "unused"], "Path is valid only with verify"),
        ("check", ["-Store", "unused"], "Store is valid only with export, import, or status"),
        ("status", ["-Store", " "], "Store must not be blank"),
    ],
)
def test_powershell_transfer_rejects_inapplicable_options(action, arguments, message, tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    powershell = shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not installed")
    script = Path(transfer.__file__).parent / "scripts/transfer.ps1"
    run = subprocess.run(
        [powershell, "-NoProfile", "-File", str(script), "-Action", action, *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode != 0
    assert message in run.stdout + run.stderr


def test_wal_changes_are_detected_before_claim(project, published):
    connection = sqlite3.connect(project / "data/progress.db")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO processing_state VALUES ('another', 'completed')")
        connection.commit()
        with pytest.raises(WorkstationGuardError, match="uncheckpointed"):
            get_workstation_lease("3080", root=project).claim()
    finally:
        connection.close()


def test_precision_contract_blocks_silent_change_on_another_pc(project, tmp_path):
    (project / "data/hardware-profile.local.json").write_text(
        json.dumps(
            {
                "profile": "3080",
                "precision": "fp16",
                "workers": 7,
            }
        )
    )
    store = tmp_path / "store"
    transfer.export_snapshot(project, store, apply=True)
    other = tmp_path / "other"
    transfer.import_snapshot(project, store, other, apply=True)
    with pytest.raises(WorkstationGuardError, match="requires fp16"):
        get_workstation_lease("4090", root=other).claim()
    assert not (other / "data/hardware-profile.local.json").exists()
    (other / "data/hardware-profile.local.json").write_text(
        json.dumps(
            {
                "profile": "4090",
                "precision": "fp16",
            }
        )
    )
    assert get_workstation_lease("4090", root=other).claim()["profile"] == "4090"


def test_missing_and_updated_story_fragments_are_reconciled_only_in_snapshot(project, tmp_path):
    import hashlib

    from story_packing import build_story_packs, story_pack_status

    records = project / "data/stories/_records"
    records.mkdir(parents=True)
    paths = []
    for number in range(2):
        source = f"source-{number}.wet.gz"
        path = records / (hashlib.sha256(source.encode()).hexdigest()[:20] + ".jsonl.gz")
        row = {
            "record_id": str(number),
            "source_file": source,
            "story": {
                "text": "Original story",
                "story_fingerprint": "example",
                "expansion_version": "old",
                "paragraphs": [{"role": "seed", "text": "Original story"}],
            },
        }
        with gzip.open(path, "wt", encoding="utf-8") as output:
            output.write(json.dumps(row) + "\n")
        paths.append(path)
    build_story_packs(project / "data/stories")
    missing_bytes = paths[0].read_bytes()
    paths[0].unlink()
    with gzip.open(paths[1], "rt", encoding="utf-8") as handle:
        changed = json.load(handle)
    changed["story"]["text"] = "Newly enriched original story"
    with gzip.open(paths[1], "wt", encoding="utf-8") as handle:
        json.dump(changed, handle)
    changed_bytes = paths[1].read_bytes()
    original_pack = (project / "data/stories/_packs/catalog.json.gz").read_bytes()
    store = tmp_path / "store"
    transfer.export_snapshot(project, store, apply=True)
    assert not paths[0].exists()
    assert paths[1].read_bytes() == changed_bytes
    assert (project / "data/stories/_packs/catalog.json.gz").read_bytes() == original_pack
    destination = tmp_path / "received"
    transfer.import_snapshot(project, store, destination, apply=True)
    assert (destination / paths[0].relative_to(project)).read_bytes() == missing_bytes
    assert (destination / paths[1].relative_to(project)).read_bytes() == changed_bytes
    assert story_pack_status(destination / "data/stories")["safe_to_pull"]


def test_existing_destination_never_overwritten(project, published, tmp_path):
    store, _ = published
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("unsynced work", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        transfer.import_snapshot(project, store, destination, apply=True)
    assert sentinel.read_text() == "unsynced work"


def test_credentials_in_compressed_data_block_export(project, tmp_path):
    secret = ("hf_" + "aBcDefGhiJkLmNoPqRsTuVwXyZ").encode()
    with gzip.open(project / "data/output/hidden.jsonl.gz", "wb") as output:
        output.write(secret)
    store = tmp_path / "store"
    with pytest.raises(ValueError, match="credential scan"):
        transfer.export_snapshot(project, store, apply=True)
    assert not (store / "control.json").exists()
    assert list((store / "snapshots").glob("*.zip")) == []
    assert not (project / transfer.CONFIG).exists()


def test_credentials_in_sqlite_text_block_export(project, tmp_path):
    with sqlite3.connect(project / "data/progress.db") as db:
        db.execute("CREATE TABLE notes (text TEXT)")
        db.execute("INSERT INTO notes VALUES (?)", ("hf_" + "aBcDefGhiJkLmNoPqRsTuVwXyZ",))
    with pytest.raises(ValueError, match="credential scan"):
        transfer.export_snapshot(project, tmp_path / "store", apply=True)


def test_rebuildable_index_cache_stays_local_without_excluding_recovery_state(project, tmp_path):
    cached = project / "data/full_sources/_index_cache/crawl/blocks/index.json.gz"
    cached.parent.mkdir(parents=True)
    with gzip.open(cached, "wb") as handle:
        handle.write(("hf_" + "aBcDefGhiJkLmNoPqRsTuVwXyZ").encode())
    for name in (
        "data/full_sources/_attempts/2013-12.json",
        "data/full_exports/2013-12/recovery_attempts.json",
    ):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"attempt_count":1}')
    result = transfer.export_snapshot(project, tmp_path / "store", apply=True)
    with zipfile.ZipFile(result["path"]) as archive:
        names = archive.namelist()
    assert not any("_index_cache" in name for name in names)
    assert "payload/data/full_sources/_attempts/2013-12.json" in names
    assert "payload/data/full_exports/2013-12/recovery_attempts.json" in names
    assert cached.exists()
    assert not transfer._included("data/full_sources/_index_cache/crawl/blocks/index.json.gz")


def test_binary_database_column_does_not_hide_text_assignments(project, tmp_path):
    with sqlite3.connect(project / "data/progress.db") as db:
        db.execute("CREATE TABLE notes (binary BLOB, text TEXT)")
        db.execute(
            "INSERT INTO notes VALUES (?, ?)", (b"\0binary", "password=" + "aB3xY9mN7pQ2rS8tU4vW6z")
        )
    with pytest.raises(ValueError, match="credential scan"):
        transfer.export_snapshot(project, tmp_path / "store", apply=True)


def test_lfs_pointer_is_not_treated_as_actual_research_data(project, tmp_path):
    (project / "data/output/story.jsonl.gz").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64 + "\nsize 100\n"
    )
    with pytest.raises(ValueError, match="LFS pointer"):
        transfer.export_snapshot(project, tmp_path / "store", apply=True)


def test_active_sources_block_export(project, tmp_path):
    with sqlite3.connect(project / "data/progress.db") as db:
        db.execute("UPDATE processing_state SET status = 'processing'")
    with pytest.raises(ValueError, match="active source leases"):
        transfer.export_snapshot(project, tmp_path / "store", apply=True)


def test_live_run_lock_blocks_export(project, tmp_path):
    from run_lock import CrawlerAlreadyRunning, CrawlerRunLock

    with CrawlerRunLock("test", project / "data/.crawler.lock"):
        with pytest.raises(CrawlerAlreadyRunning):
            transfer.export_snapshot(project, tmp_path / "store", apply=True)


def test_store_cannot_be_inside_project(project):
    with pytest.raises(ValueError, match="outside"):
        transfer.export_snapshot(project, project / "backup")


def test_windows_reparse_points_are_rejected_without_following_them(project, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    original = Path.lstat

    def metadata(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == project / "main.py":
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result

    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(ValueError, match="links cannot enter"):
        transfer.inventory(project)


def test_git_ownership_must_be_released_before_switching(project, tmp_path):
    (project / "data/.workstation-owner.local.json").write_text("{}")
    with pytest.raises(ValueError, match="Git workstation lease"):
        transfer.export_snapshot(project, tmp_path / "store", apply=True)


def test_checksum_failure_leaves_no_destination(project, published, tmp_path):
    store, result = published

    def damage(output, info, content):
        if info.filename == "payload/main.py":
            content = content.replace(b"example", b"damaged")
        output.writestr(info, content)

    rewrite(result["path"], damage)
    destination = tmp_path / "received"
    with pytest.raises(ValueError, match="checksum"):
        transfer.import_snapshot(project, store, destination, apply=True)
    assert not destination.exists()
    assert not list(tmp_path.glob(".hometown-import-*"))


@pytest.mark.parametrize(
    "name",
    [
        "../outside.py",
        "/absolute.py",
        "data/../bad.py",
        "data/CON.json",
        "data/a:stream.json",
        "data/a\\b.json",
        "data/bad. ",
    ],
)
def test_unsafe_paths_rejected(name):
    with pytest.raises(ValueError):
        transfer._safe_name(name)


def test_unlisted_archive_entry_is_rejected(published):
    _, result = published
    with zipfile.ZipFile(result["path"], "a") as output:
        output.writestr("payload/../outside.py", "unexpected")
    with pytest.raises(ValueError, match="entries"):
        transfer.verify_snapshot(result["path"])


def test_symlink_entry_rejected(published):
    _, result = published

    def symlink(output, info, content):
        if info.filename == "payload/main.py":
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(info, content)

    rewrite(result["path"], symlink)
    with pytest.raises(ValueError, match="special file"):
        transfer.verify_snapshot(result["path"])


def test_case_colliding_entries_rejected(published):
    _, result = published
    with zipfile.ZipFile(result["path"], "a") as output:
        output.writestr("payload/MAIN.py", b"collision")
    with pytest.raises(ValueError, match="case-colliding"):
        transfer.verify_snapshot(result["path"])


def test_malformed_manifest_is_rejected(published):
    _, result = published

    def malformed(output, info, content):
        output.writestr(info, b"[]" if info.filename == "manifest.json" else content)

    rewrite(result["path"], malformed)
    with pytest.raises(ValueError, match="unsupported snapshot"):
        transfer.verify_snapshot(result["path"])


def test_manifest_size_is_bounded(published, monkeypatch):
    _, result = published
    monkeypatch.setattr(transfer, "MAX_MANIFEST_BYTES", 1)
    with pytest.raises(ValueError, match="oversized"):
        transfer.verify_snapshot(result["path"])


def test_wrong_store_identity_rejected_even_for_preview(project, published, tmp_path):
    store, _ = published
    control = json.loads((store / "control.json").read_text())
    control["store_id"] = "f" * 32
    (store / "control.json").write_text(json.dumps(control))
    with pytest.raises(ValueError, match="identity mismatch"):
        transfer.import_snapshot(project, store, tmp_path / "received")


def test_export_failure_does_not_replace_last_good_snapshot(project, published, monkeypatch):
    store, old = published
    before = (store / "control.json").read_bytes()
    original = transfer._scan

    def interrupted(path, name):
        if name == "main.py":
            raise OSError("simulated disk error")
        return original(path, name)

    monkeypatch.setattr(transfer, "_scan", interrupted)
    with pytest.raises(OSError, match="disk error"):
        transfer.export_snapshot(project, store, apply=True)
    assert (store / "control.json").read_bytes() == before
    assert (store / "snapshots" / (old["snapshot_id"] + ".zip")).exists()
    assert not list((store / "snapshots").glob("*.tmp"))


def test_local_health_needs_no_git(project, published, monkeypatch):
    import project_health

    monkeypatch.setattr(project_health, "_git", lambda *args, **kwargs: pytest.fail("Git invoked"))
    result = project_health.git_health(project)
    assert result["transport"] == "folder"
    assert result["local_handoff"]["ready"]
    (project / "data/output/en.jsonl").write_text("changed data")
    assert not project_health.git_health(project)["local_handoff"]["ready"]


def test_local_check_does_not_need_git(project, monkeypatch):
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("Git invoked"))
    assert transfer.check_local_files(project)["valid"]


def test_store_mutex_is_exclusive(project, published):
    store, _ = published
    with transfer._store_lock(store):
        with pytest.raises(ValueError, match="busy"):
            transfer.export_snapshot(project, store, apply=True)


def test_stale_ownership_update_cannot_replace_another_owner(project, published):
    lease = get_workstation_lease("3080", root=project)
    revision, _ = lease._read_remote()
    lease._publish({"state": "active", "owner_id": "first"}, revision)
    with pytest.raises(WorkstationGuardError, match="changed ownership"):
        lease._publish({"state": "active", "owner_id": "second"}, revision)
    assert lease._read_remote()[1]["owner_id"] == "first"


def test_folder_lease_prevents_competing_pcs_even_after_expiry(project, published, tmp_path):
    store, _ = published
    other = tmp_path / "other"
    transfer.import_snapshot(project, store, other, apply=True)
    now = [datetime.now(timezone.utc)]
    first = transfer.FolderWorkstationLease("3080", root=project, host="first", now=lambda: now[0])
    second = transfer.FolderWorkstationLease("4090", root=other, host="second", now=lambda: now[0])
    first.claim()
    now[0] += timedelta(days=2)
    with pytest.raises(WorkstationGuardError, match="does not auto-expire"):
        second.claim()
    with pytest.raises(WorkstationGuardError, match="never force-stolen"):
        second.claim(force_recovery=True)
    first.release()
    assert second.claim()["profile"] == "4090"


def test_modified_work_is_retained_and_export_releases_ownership(project, published, tmp_path):
    store, old = published
    other = tmp_path / "other"
    transfer.import_snapshot(project, store, other, apply=True)
    with get_workstation_lease("3080", root=project):
        (project / "data/output/en.jsonl").write_text('{"text":"New human-reviewed story."}\n')
    assert get_workstation_lease("3080", root=project).status()["active"]
    with pytest.raises(WorkstationGuardError, match="uncheckpointed"):
        get_workstation_lease("3080", root=project).release()
    fresh = transfer.export_snapshot(project, store, apply=True)
    assert fresh["snapshot_id"] != old["snapshot_id"]
    assert not get_workstation_lease("3080", root=project).status()["active"]
    assert not (project / transfer.OWNER).exists()
    with pytest.raises(WorkstationGuardError, match="stale"):
        get_workstation_lease("4090", root=other).claim()
    with pytest.raises(ValueError, match="stale"):
        transfer.export_snapshot(other, store, apply=True)


def test_claim_and_import_fail_when_store_is_owned(project, published, tmp_path):
    store, _ = published
    lease = get_workstation_lease("3080", root=project)
    lease.claim()
    with pytest.raises(ValueError, match="owning workstation"):
        transfer.import_snapshot(project, store, tmp_path / "new", apply=True)


def test_unrelated_checkout_cannot_replace_store(project, published, tmp_path):
    store, _ = published
    (project / transfer.CONFIG).unlink()
    with pytest.raises(ValueError, match="receive"):
        transfer.export_snapshot(project, store, apply=True)


def test_code_changes_require_new_snapshot(project, published):
    (project / "main.py").write_text("# modified\n")
    with pytest.raises(WorkstationGuardError, match="code changed"):
        get_workstation_lease("3080", root=project).claim()


def test_default_git_transport_and_bad_config_do_not_fall_back(project):
    assert type(get_workstation_lease("3080", root=project)) is WorkstationLease
    (project / transfer.CONFIG).write_text(json.dumps({"mode": "unknown"}))
    with pytest.raises(WorkstationGuardError, match="invalid local"):
        get_workstation_lease("3080", root=project)


def test_folder_mode_never_invokes_git(project, published, monkeypatch):
    import subprocess

    def reject(*args, **kwargs):
        pytest.fail("Git was invoked in folder mode")

    monkeypatch.setattr(subprocess, "run", reject)
    with get_workstation_lease("3080", root=project):
        pass


def test_missing_store_does_not_silently_release_or_fall_back(project, published):
    store, _ = published
    lease = get_workstation_lease("3080", root=project)
    lease.claim()
    (store / "control.json").rename(store / "control.offline")
    with pytest.raises(OSError):
        lease.renew_if_due(force=True)
    assert (project / transfer.OWNER).exists()
