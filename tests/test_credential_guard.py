import subprocess
import sys
from pathlib import Path

from credential_guard import is_sensitive_path, scan_content

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def test_sensitive_paths_cover_common_local_credentials():
    assert is_sensitive_path(".env")
    assert is_sensitive_path("credentials/service.json")
    assert is_sensitive_path("config/client_secret_local.json")
    assert is_sensitive_path("private/signing.key")
    assert not is_sensitive_path(".env.example")
    assert not is_sensitive_path("credential_guard.py")


def test_content_scan_detects_known_token_without_echoing_it():
    token = ("gh" + "p_" + "A" * 36).encode()

    findings = scan_content("settings.txt", b"TOKEN=" + token)

    assert [(finding.rule, finding.line) for finding in findings] == [
        ("GitHub token", 1),
    ]


def test_content_scan_detects_high_entropy_generic_assignment():
    content = ("client" + '_secret = "A9bcD2efG3hiJ4klM5noP6qr"\n').encode()

    findings = scan_content("settings.py", content)

    assert [finding.rule for finding in findings] == ["credential assignment"]


def test_content_scan_allows_documented_placeholders():
    content = b'api_key = "replace-me-with-your-key"\n'

    assert scan_content(".env.example", content) == []


def test_staged_scan_unstages_finding_without_deleting_local_file(tmp_path):
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.email", "tests@example.invalid")
    _git(tmp_path, "config", "user.name", "Hometown XR Tests")
    readme = tmp_path / "README.md"
    readme.write_text("baseline\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "--quiet", "-m", "baseline")

    settings = tmp_path / "settings.txt"
    value = "A9bcD2efG3hiJ4klM5noP6qr"
    settings.write_text(f"api_key={value}\n", encoding="utf-8")
    _git(tmp_path, "add", "settings.txt")

    process = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "credential_guard.py"),
            "--scope",
            "staged",
            "--unstage",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert process.returncode == 1
    assert value not in process.stdout
    assert value not in process.stderr
    assert _git(tmp_path, "diff", "--cached", "--name-only").stdout == ""
    assert settings.read_text(encoding="utf-8") == f"api_key={value}\n"
