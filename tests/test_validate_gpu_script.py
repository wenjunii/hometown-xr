import json
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PIN = "430a82aa88a402df71b337b43ec5f68ac78921ef"


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def launcher(tmp_path):
    powershell = shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not installed")
    script = PROJECT_ROOT / "scripts/validate-gpu.ps1"
    store = tmp_path / "prepared store"
    store.mkdir()
    control = {"store_id": "test-store", "snapshot_id": "test-snapshot"}
    (store / "control.json").write_text(json.dumps(control), encoding="utf-8")
    receipt = {**control, "candidate": {"git_commit": RUNTIME_PIN}}
    (store / "PREPARATION-2026-11-10.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    return powershell, script, store, tmp_path / "new validation checkout"


def invoke(launcher, prefix="", *, apply=False, profile="5090", destination=None):
    powershell, script, store, default_destination = launcher
    destination = destination or default_destination
    command = (
        prefix
        + f"\n& {ps_quote(script)} -Store {ps_quote(store)} -Profile {profile} "
        + f"-Destination {ps_quote(destination)}"
        + (" -Apply" if apply else "")
    )
    return subprocess.run(
        [powershell, "-NoProfile", "-Command", command],
        cwd=store.parent, capture_output=True, text=True, timeout=30,
    )


def test_preview_keeps_exact_pin_and_does_not_create_checkout(launcher):
    run = invoke(launcher, "function git { throw 'Git must not run in preview' }")
    assert run.returncode == 0, run.stdout + run.stderr
    preview = json.loads(run.stdout)
    assert preview["candidate_commit"] == RUNTIME_PIN
    assert preview["required_precision"] == "fp16"
    assert preview["applied"] is False
    assert not launcher[3].exists()


@pytest.mark.parametrize(
    ("gpu", "exit_code"),
    [
        ("NVIDIA GeForce RTX 4090", "0"),
        ("NVIDIA GeForce RTX 5090", "9"),
        ("NVIDIA GeForce RTX 5090", "$null"),
        ("", "0"),
    ],
)
def test_gpu_guard_rejects_bad_or_missing_native_result(launcher, gpu, exit_code):
    prefix = (
        f"function nvidia-smi {{ $global:LASTEXITCODE = {exit_code}; {ps_quote(gpu)} }}\n"
        "function git { throw 'Git must not run after GPU rejection' }"
    )
    run = invoke(launcher, prefix, apply=True)
    assert run.returncode != 0
    assert "GPU precheck failed" in run.stdout + run.stderr
    assert "Git must not run" not in run.stdout + run.stderr
    assert not launcher[3].exists()


@pytest.mark.parametrize("profile", ["4090", "5090"])
def test_gpu_success_overwrites_stale_exit_status_before_clone(launcher, profile):
    prefix = (
        "$global:LASTEXITCODE = 37\n"
        "function nvidia-smi { $global:LASTEXITCODE = 0; "
        f"'NVIDIA GeForce RTX {profile}' }}\n"
        "function git { param([string]$Action) "
        "if ($Action -eq 'lfs') { $global:LASTEXITCODE = 0; return } "
        "throw 'CLONE_REACHED_WITHOUT_NETWORK' }"
    )
    run = invoke(launcher, prefix, apply=True, profile=profile)
    assert run.returncode != 0
    assert "CLONE_REACHED_WITHOUT_NETWORK" in run.stdout + run.stderr
    assert "GPU precheck failed" not in run.stdout + run.stderr
    assert not launcher[3].exists()


def test_existing_destination_is_preserved(launcher):
    destination = launcher[3]
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("existing work", encoding="utf-8")
    run = invoke(launcher, apply=True)
    assert run.returncode != 0
    assert "destination already exists" in run.stdout + run.stderr
    assert sentinel.read_text(encoding="utf-8") == "existing work"


def test_gpu_guard_checks_first_physical_device(launcher):
    prefix = (
        "function nvidia-smi { $global:LASTEXITCODE = 0; "
        "'NVIDIA GeForce RTX 4090'; 'NVIDIA GeForce RTX 5090' }\n"
        "function git { throw 'Git must not run after GPU rejection' }"
    )
    run = invoke(launcher, prefix, apply=True)
    assert run.returncode != 0
    assert "GPU precheck failed" in run.stdout + run.stderr
    assert not launcher[3].exists()


def test_destination_inside_usb_is_rejected(launcher):
    run = invoke(launcher, apply=True, destination=launcher[2] / "checkout")
    assert run.returncode != 0
    assert "must not be inside" in run.stdout + run.stderr
    assert not (launcher[2] / "checkout").exists()


def test_receipt_cannot_redirect_to_moving_or_old_runtime(launcher):
    receipt = launcher[2] / "PREPARATION-2026-11-10.json"
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["candidate"]["git_commit"] = "main"
    receipt.write_text(json.dumps(data), encoding="utf-8")
    run = invoke(launcher)
    assert run.returncode != 0
    assert "prepared runtime does not match" in run.stdout + run.stderr
    assert not launcher[3].exists()


def test_missing_snapshot_identity_is_not_treated_as_a_match(launcher):
    for name in ("control.json", "PREPARATION-2026-11-10.json"):
        path = launcher[2] / name
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["snapshot_id"]
        path.write_text(json.dumps(data), encoding="utf-8")
    run = invoke(launcher)
    assert run.returncode != 0
    assert "store identity, snapshot, or prepared runtime" in run.stdout + run.stderr
    assert not launcher[3].exists()
