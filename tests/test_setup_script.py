import shutil
import subprocess
from pathlib import Path

import pytest


def test_5090_setup_stops_before_environment_changes():
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is not installed")
    root = Path(__file__).resolve().parents[1]
    interpreter = root / ".venv/Scripts/python.exe"
    before = interpreter.stat().st_mtime_ns if interpreter.exists() else None
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(root / "scripts/setup.ps1"), "-Profile", "5090"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode != 0
    assert "5090 setup is security-blocked" in result.stdout + result.stderr
    assert (interpreter.stat().st_mtime_ns if interpreter.exists() else None) == before
