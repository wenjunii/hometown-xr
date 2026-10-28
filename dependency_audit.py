"""Audit every pinned manifest, failing closed on missing vulnerability coverage."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from config import PROJECT_ROOT
from dependency_profiles import validate_security_policy

DEFAULT_MANIFESTS = (
    "requirements.txt",
    "requirements-lock.txt",
    "requirements-lock-5090.txt",
    "requirements-test.txt",
)


def audit_pins(path: Path, ancestors: tuple[Path, ...] = ()) -> dict[str, str]:
    """Read exact pins, including nested files and inactive platform markers."""
    source = path.resolve()
    if source in ancestors:
        raise ValueError(f"cyclic requirements include: {source.name}")
    pins: dict[str, str] = {}
    for raw in source.read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("--extra-index-url ", "--index-url ")):
            continue
        if line.startswith(("-r ", "--requirement ")):
            nested = source.parent / line.split(maxsplit=1)[1]
            additions = audit_pins(nested, (*ancestors, source))
        else:
            requirement = Requirement(line)
            specifiers = list(requirement.specifier)
            if (
                requirement.url or len(specifiers) != 1
                or specifiers[0].operator != "==" or "*" in specifiers[0].version
            ):
                raise ValueError(f"{source.name} requires exact pins: {line}")
            name = canonicalize_name(requirement.name)
            version = Version(specifiers[0].version)
            if version.local and not (
                name == "torch" and version.local.startswith("cu") and version.local[2:].isdigit()
            ):
                raise ValueError(f"unsupported local audit version: {line}")
            # CUDA wheel suffixes share the upstream torch vulnerability record.
            additions = {
                name: version.public
            }
        for package, version in additions.items():
            if package in pins and pins[package] != version:
                raise ValueError(f"conflicting audit pins for {package}")
            pins[package] = version
    if not pins:
        raise ValueError(f"{source.name} has no auditable pins")
    return pins


def evaluate_audit_report(report: dict, policy: dict, today: date | None = None) -> dict:
    current_date = today or datetime.now(timezone.utc).date()
    checked_policy = validate_security_policy(policy, current_date)
    errors = list(checked_policy["errors"])
    raw_allowances = policy.get("temporarily_allowed_vulnerabilities", {})
    allowed = {
        canonicalize_name(package): set(identifiers)
        for package, identifiers in raw_allowances.items()
        if isinstance(package, str) and isinstance(identifiers, list)
        and all(isinstance(identifier, str) for identifier in identifiers)
    } if isinstance(raw_allowances, dict) else {}
    findings = []
    unexpected_packages = []
    unexpected_vulnerabilities = []
    observed: dict[str, set[str]] = {}
    dependencies = report.get("dependencies") if isinstance(report, dict) else None
    if not isinstance(dependencies, list):
        errors.append("audit report has no dependency list")
        dependencies = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            errors.append("invalid dependency entry in audit report")
            continue
        package = str(dependency.get("name", "unknown"))
        if dependency.get("skip_reason"):
            errors.append(f"audit skipped {package}: {dependency['skip_reason']}")
            continue
        vulnerabilities = dependency.get("vulns")
        if (
            not dependency.get("name") or not dependency.get("version")
            or not isinstance(vulnerabilities, list)
            or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                   or not item["id"] for item in vulnerabilities)
        ):
            errors.append(f"incomplete audit entry for {package}")
            continue
        normalized_package = canonicalize_name(package)
        exception_versions = policy.get("exception_versions", {})
        if (
            normalized_package in allowed and isinstance(exception_versions, dict)
            and dependency["version"] not in exception_versions.get(normalized_package, [])
        ):
            errors.append(
                f"exception does not cover {package}=={dependency['version']}"
            )
        if not vulnerabilities:
            continue
        identifiers = sorted({item["id"] for item in vulnerabilities})
        observed.setdefault(normalized_package, set()).update(identifiers)
        row = {
            "package": package,
            "version": dependency["version"],
            "vulnerability_ids": identifiers,
        }
        findings.append(row)
        package_allowlist = allowed.get(normalized_package, set())
        if normalized_package not in allowed:
            unexpected_packages.append(row)
        unreviewed = sorted(set(identifiers) - package_allowlist)
        if unreviewed:
            unexpected_vulnerabilities.append({**row, "vulnerability_ids": unreviewed})
    if unexpected_vulnerabilities:
        errors.append(
            "unreviewed vulnerabilities: "
            + ", ".join(
                f"{row['package']}:{identifier}"
                for row in unexpected_vulnerabilities
                for identifier in row["vulnerability_ids"]
            )
        )
    return {
        "valid": not errors,
        "security_clean": not errors and not findings,
        "review_by": policy.get("review_by"),
        "days_until_review": checked_policy["days_until_review"],
        "vulnerable_packages": findings,
        "unexpected_packages": unexpected_packages,
        "unexpected_vulnerabilities": unexpected_vulnerabilities,
        "unused_allowances": {
            package: sorted(identifiers - observed.get(package, set()))
            for package, identifiers in allowed.items()
            if identifiers - observed.get(package, set())
        },
        "errors": errors,
    }


def run_pip_audit(requirements: Path) -> dict:
    pins = audit_pins(requirements)
    with tempfile.TemporaryDirectory(prefix="hometown-xr-audit-") as temporary:
        output = Path(temporary) / "audit.json"
        normalized = Path(temporary) / "requirements.txt"
        normalized.write_text(
            "".join(f"{name}=={version}\n" for name, version in sorted(pins.items())),
            encoding="utf-8",
        )
        process = subprocess.run(
            [
                sys.executable, "-m", "pip_audit", "-r", str(normalized),
                "--no-deps", "--disable-pip", "--format", "json", "--output", str(output),
            ],
            check=False,
            timeout=300,
        )
        if process.returncode not in (0, 1) or not output.exists():
            raise RuntimeError(f"pip-audit failed with exit code {process.returncode}")
        report = json.loads(output.read_text(encoding="utf-8"))
        dependencies = report.get("dependencies") if isinstance(report, dict) else None
        if not isinstance(dependencies, list):
            raise ValueError("pip-audit returned an invalid dependency list")
        if any(not isinstance(item, dict) for item in dependencies):
            raise ValueError("pip-audit returned an invalid dependency entry")
        covered = {
            (canonicalize_name(item["name"]), str(item["version"]))
            for item in dependencies if isinstance(item, dict)
            and isinstance(item.get("name"), str) and item.get("version")
            and not item.get("skip_reason") and isinstance(item.get("vulns"), list)
        }
        missing = sorted(f"{name}=={version}" for name, version in pins.items()
                         if (name, version) not in covered)
        if missing:
            raise RuntimeError("missing audit coverage: " + ", ".join(missing))
        if process.returncode == 1 and not any(item.get("vulns") for item in dependencies):
            raise RuntimeError("pip-audit failed without reporting vulnerabilities")
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", action="append", help="Repeat to select manifests.")
    parser.add_argument("--policy", default=str(PROJECT_ROOT / ".github/dependency-policy.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    results = {}
    try:
        policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
        checked_policy = validate_security_policy(policy, datetime.now(timezone.utc).date())
        if checked_policy["errors"]:
            raise ValueError("; ".join(checked_policy["errors"]))
        for name in args.requirements or DEFAULT_MANIFESTS:
            path = PROJECT_ROOT / name
            report = run_pip_audit(path)
            results[name] = {
                **evaluate_audit_report(report, policy),
                "audited_pins": len(report["dependencies"]),
            }
        result = {
            "valid": all(item["valid"] for item in results.values()),
            "security_clean": all(item["security_clean"] for item in results.values()),
            "manifests": results,
        }
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result = {"valid": False, "security_clean": False, "manifests": results, "errors": [str(exc)]}
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
