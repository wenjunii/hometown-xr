"""Block credential-like files and high-confidence secrets from Git commits."""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_KNOWN_SECRET_PATTERNS = (
    (
        "private key",
        re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?" + rb"PRIVATE KEY-----"),
    ),
    (
        "GitHub token",
        re.compile(rb"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"),
    ),
    ("AWS access key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Google API key", re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("Slack token", re.compile(rb"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    (
        "OpenAI API key",
        re.compile(rb"\bsk-(?!ant-)(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    ("Anthropic API key", re.compile(rb"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("Hugging Face token", re.compile(rb"\bhf_[A-Za-z0-9]{20,}\b")),
    (
        "Stripe secret key",
        re.compile(rb"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    ),
)

_GENERIC_ASSIGNMENT = re.compile(
    r"(?im)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|passwd)\b\s*[=:]\s*[\"']?([A-Za-z0-9_./+=-]{16,})"
)
_CREDENTIAL_URL = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]{2,}://[^\s:/?#]+:([^@\s/]{4,})@"
)
_PLACEHOLDER_PARTS = (
    "changeme",
    "dummy",
    "example",
    "fake",
    "not-a-real",
    "placeholder",
    "redacted",
    "replace-me",
    "your-key",
    "your-token",
)
_BINARY_SUFFIXES = {
    ".7z",
    ".db",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".parquet",
    ".pdf",
    ".png",
    ".pyc",
    ".sqlite",
    ".webp",
    ".zip",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


def _run_git(*arguments: str) -> bytes:
    process = subprocess.run(
        ["git", *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.returncode != 0:
        message = process.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or f"git {' '.join(arguments)} failed")
    return process.stdout


def _split_paths(raw: bytes) -> list[str]:
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in raw.split(b"\0")
        if item
    ]


def candidate_paths(scope: str) -> list[str]:
    if scope == "staged":
        raw = _run_git(
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
            "-z",
        )
    elif scope == "tracked":
        raw = _run_git("ls-files", "-z")
    else:
        raw = _run_git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted(set(_split_paths(raw)))


def is_sensitive_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    parts = normalized.split("/")
    leaf = parts[-1]
    if leaf == ".env.example":
        return False
    if any(part == ".env" or part.startswith(".env.") for part in parts):
        return True
    if any(
        part in {"credential", "credentials", "secret", "secrets"}
        for part in parts[:-1]
    ):
        return True
    if re.fullmatch(
        r"(?:credentials?|secrets?)(?:\.(?:local\.)?(?:json|ya?ml|toml|ini|txt))?",
        leaf,
    ):
        return True
    if re.fullmatch(r"(?:id_rsa|id_ed25519)(?:\..*)?", leaf):
        return True
    if re.fullmatch(r"service[-_]?account.*\.json", leaf):
        return True
    if re.fullmatch(r"(?:client[-_]?secret|oauth|tokens?).*\.json", leaf):
        return True
    if leaf in {".git-credentials", ".netrc", "_netrc", ".npmrc", ".pypirc"}:
        return True
    return Path(leaf).suffix.lower() in {
        ".jks",
        ".key",
        ".keytab",
        ".kdbx",
        ".keystore",
        ".p12",
        ".pem",
        ".pfx",
    }


def _line_number(content: bytes, offset: int) -> int:
    return content.count(b"\n", 0, offset) + 1


def _entropy(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return (
        any(part in lowered for part in _PLACEHOLDER_PARTS)
        or lowered.startswith(("${", "{{", "<"))
        or len(set(value)) < 6
    )


def scan_content(path: str, content: bytes) -> list[Finding]:
    findings: list[Finding] = []
    for rule, pattern in _KNOWN_SECRET_PATTERNS:
        match = pattern.search(content)
        if match:
            findings.append(Finding(path, _line_number(content, match.start()), rule))

    suffix = Path(path).suffix.lower()
    if suffix in _BINARY_SUFFIXES or b"\0" in content[:8192]:
        return findings

    text = content.decode("utf-8", errors="replace")
    for rule, pattern in (
        ("credential assignment", _GENERIC_ASSIGNMENT),
        ("credential-bearing URL", _CREDENTIAL_URL),
    ):
        for match in pattern.finditer(text):
            value = match.group(1)
            if _looks_like_placeholder(value):
                continue
            if rule == "credential assignment" and _entropy(value) < 3.25:
                continue
            offset = len(text[: match.start()].encode("utf-8", errors="replace"))
            findings.append(Finding(path, _line_number(content, offset), rule))
            break
    return findings


def _read_content(root: Path, path: str, scope: str) -> bytes:
    if scope == "staged":
        return _run_git("show", f":{path}")
    return (root / Path(path)).read_bytes()


def scan_repository(root: Path, scope: str) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    scanned = 0
    for path in candidate_paths(scope):
        if is_sensitive_path(path):
            findings.append(Finding(path, 1, "credential-like path"))
            continue
        try:
            content = _read_content(root, path, scope)
        except FileNotFoundError:
            continue
        scanned += 1
        findings.extend(scan_content(path, content))
    return findings, scanned


def unstage_findings(findings: Iterable[Finding]) -> None:
    for path in sorted({finding.path for finding in findings}):
        _run_git("restore", "--staged", "--", path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope",
        choices=("staged", "tracked", "worktree"),
        default="worktree",
        help="Files to inspect (default: tracked and untracked non-ignored worktree files).",
    )
    parser.add_argument(
        "--unstage",
        action="store_true",
        help="Unstage files with findings; valid only with --scope staged.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.unstage and args.scope != "staged":
        print("--unstage requires --scope staged", file=sys.stderr)
        return 2

    root = Path(_run_git("rev-parse", "--show-toplevel").decode().strip())
    findings, scanned = scan_repository(root, args.scope)
    if findings:
        if args.unstage:
            unstage_findings(findings)
        print(f"Credential scan blocked {len(findings)} finding(s):", file=sys.stderr)
        for finding in findings:
            print(f"  {finding.path}:{finding.line} [{finding.rule}]", file=sys.stderr)
        if args.unstage:
            print(
                "Affected files were removed from the staging area and remain local.",
                file=sys.stderr,
            )
        return 1

    print(f"Credential scan passed: {scanned} {args.scope} file(s) inspected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
