import json
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def validation_checkout(tmp_path):
    powershell = shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not installed")
    root = tmp_path / "isolated candidate"
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts/validate-runtime.ps1"
    shutil.copy2(PROJECT_ROOT / "scripts/validate-runtime.ps1", script)
    return root, script, powershell


def test_validation_help_does_not_require_or_create_environment(validation_checkout):
    root, script, powershell = validation_checkout
    quoted = str(script).replace("'", "''")
    run = subprocess.run(
        [powershell, "-NoProfile", "-Command",
         f"Get-Help '{quoted}' -Full | ConvertTo-Json -Depth 8"],
        cwd=root.parent, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    help_text = json.loads(run.stdout)
    assert "isolated runtime candidate" in help_text["Synopsis"]
    rendered = json.dumps(help_text)
    assert "FP16" in rendered
    assert "dry-run imported" in rendered
    assert "-Profile 3080 -Precision fp16" in rendered
    assert "-Profile 5090 -Precision fp16" in rendered
    assert not (root / ".venv").exists()
    assert not (root / "data").exists()


def test_validation_refuses_missing_candidate_environment(validation_checkout):
    root, script, powershell = validation_checkout
    run = subprocess.run(
        [powershell, "-NoProfile", "-File", str(script), "-Profile", "3080"],
        cwd=root.parent, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode != 0
    assert "isolated candidate checkout" in run.stdout + run.stderr
    assert not (root / ".venv").exists()
    assert not (root / "data").exists()
