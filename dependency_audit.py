"""Run pip-audit while enforcing the repository's dated vulnerability policy."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from config import PROJECT_ROOT


def _vulnerability_allowlist(policy: dict) -> dict[str, set[str]]:
    raw = policy.get("temporarily_allowed_vulnerabilities", {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(package).lower(): {str(identifier) for identifier in identifiers}
        for package, identifiers in raw.items()
        if isinstance(identifiers, list)
    }


def evaluate_audit_report(report: dict, policy: dict, today: date | None = None) -> dict:
    current_date = today or datetime.now(timezone.utc).date()
    review_by = date.fromisoformat(str(policy["review_by"]))
    allowed = _vulnerability_allowlist(policy)
    findings = []
    unexpected_packages = []
    unexpected_vulnerabilities = []
    observed: dict[str, set[str]] = {}
    for dependency in report.get("dependencies", []):
        vulnerabilities = dependency.get("vulns") or []
        if not vulnerabilities:
            continue
        package = str(dependency.get("name", "unknown"))
        normalized_package = package.lower()
        identifiers = sorted(
            {str(item.get("id", "unknown")) for item in vulnerabilities}
        )
        observed.setdefault(normalized_package, set()).update(identifiers)
        row = {
            "package": package,
            "version": dependency.get("version"),
            "vulnerability_ids": identifiers,
        }
        findings.append(row)
        package_allowlist = allowed.get(normalized_package)
        if package_allowlist is None:
            unexpected_packages.append(row)
        unreviewed = sorted(set(identifiers) - (package_allowlist or set()))
        if unreviewed:
            unexpected_vulnerabilities.append(
                {
                    "package": package,
                    "version": dependency.get("version"),
                    "vulnerability_ids": unreviewed,
                }
            )

    errors = []
    if current_date > review_by:
        errors.append(f"vulnerability exception expired on {review_by.isoformat()}")
    if unexpected_vulnerabilities:
        errors.append(
            "unreviewed vulnerabilities: "
            + ", ".join(
                f"{row['package']}:{identifier}"
                for row in unexpected_vulnerabilities
                for identifier in row["vulnerability_ids"]
            )
        )
    unused_allowances = {
        package: sorted(identifiers - observed.get(package, set()))
        for package, identifiers in allowed.items()
        if identifiers - observed.get(package, set())
    }
    return {
        "valid": not errors,
        "review_by": review_by.isoformat(),
        "days_until_review": (review_by - current_date).days,
        "vulnerable_packages": findings,
        "unexpected_packages": unexpected_packages,
        "unexpected_vulnerabilities": unexpected_vulnerabilities,
        "unused_allowances": unused_allowances,
        "errors": errors,
    }


def run_pip_audit(requirements: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="hometown-xr-audit-") as temporary:
        output = Path(temporary) / "audit.json"
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "-r",
                str(requirements),
                "--no-deps",
                "--format",
                "json",
                "--output",
                str(output),
            ],
            check=False,
        )
        if not output.exists():
            raise RuntimeError(f"pip-audit failed with exit code {process.returncode}")
        return json.loads(output.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", default="requirements.txt")
    parser.add_argument(
        "--policy",
        default=str(PROJECT_ROOT / ".github" / "dependency-policy.json"),
    )
    args = parser.parse_args(argv)
    policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
    report = run_pip_audit(Path(args.requirements))
    result = evaluate_audit_report(report, policy)
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
