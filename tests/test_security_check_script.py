import json
import os
import shutil
import subprocess
import venv
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scan_checkout(tmp_path):
    powershell = shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not installed")
    root = tmp_path / "code checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "scripts/security-check.ps1", root / "scripts")
    shutil.copy2(PROJECT_ROOT / "credential_guard.py", root)
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    return root, powershell


@pytest.mark.parametrize("scope", ["staged", "worktree"])
@pytest.mark.parametrize("sensitive", [False, True])
def test_scan_without_gpu_environment_preserves_index(scan_checkout, scope, sensitive):
    root, powershell = scan_checkout
    name = ".env" if sensitive else "README.md"
    (root / name).write_text("local example\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", name], cwd=root, check=True)
    index = root / ".git/index"
    before = index.read_bytes()

    run = subprocess.run(
        [powershell, "-NoProfile", "-File", str(root / "scripts/security-check.ps1"),
         "-Scope", scope],
        cwd=root.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )

    output = run.stdout + run.stderr
    assert (run.returncode != 0) == sensitive, output
    assert ("Credential scan blocked" if sensitive else "Credential scan passed") in output
    assert index.read_bytes() == before
    assert not (root / ".venv").exists()
    assert not (root / "data").exists()


def test_scan_prefers_existing_environment(scan_checkout):
    root, powershell = scan_checkout
    environment = root / ".venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / "Scripts/python.exe"
    if os.name != "nt":
        python.parent.mkdir()
        python.symlink_to(environment / "bin/python")
    (root / "credential_guard.py").write_text(
        "import json, sys\n"
        "print(json.dumps({'prefix': sys.prefix, 'args': sys.argv[1:]}))\n",
        encoding="utf-8",
    )

    run = subprocess.run(
        [powershell, "-NoProfile", "-File", str(root / "scripts/security-check.ps1"),
         "-Scope", "staged"],
        cwd=root.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    result = json.loads(run.stdout)
    assert Path(result["prefix"]).resolve() == environment.resolve()
    assert result["args"] == ["--scope", "staged"]
    assert not (root / "data").exists()
