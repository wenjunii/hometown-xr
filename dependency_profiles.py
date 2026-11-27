"""Validate dependency locks shared by the three GPU workstation profiles."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from config import PROJECT_ROOT

EXPECTED_PINS = {
    "project": {
        "sentence-transformers": "5.6.1",
        "torch": "2.13.0",
        "transformers": "5.10.4",
    },
    "legacy": {
        "sentence-transformers": "5.6.1",
        "torch": "2.13.0+cu130",
        "transformers": "5.10.4",
    },
    "5090": {
        "sentence-transformers": "5.6.1",
        "torch": "2.13.0+cu130",
        "transformers": "5.10.4",
    },
}

MIGRATION_GATED_DEPENDENCIES = frozenset(
    {
        "fasttext-wheel",
        "ftfy",
        "huggingface-hub",
        "mpmath",
        "numpy",
        "pyarrow",
        "regex",
        "safetensors",
        "scikit-learn",
        "scipy",
        "sentence-transformers",
        "sympy",
        "tokenizers",
        "torch",
        "transformers",
    }
)


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip().lower())


def read_requirements(path: str | Path) -> tuple[dict[str, str], list[str]]:
    source = Path(path)
    pins: dict[str, str] = {}
    options: list[str] = []
    for raw_line in source.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--"):
            options.append(line)
            continue
        if line.startswith("-r "):
            continue
        requirement = line.split(";", maxsplit=1)[0].strip()
        name, separator, version = requirement.partition("==")
        if not separator:
            raise ValueError(f"{source} contains an unpinned requirement: {line}")
        pins[_normalized_name(name)] = version.strip()
    return pins, options


def read_project_requirements(path: str | Path) -> dict[str, str]:
    source = Path(path)
    with source.open("rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    pins: dict[str, str] = {}
    for dependency in dependencies:
        name, separator, version = dependency.partition("==")
        if not separator:
            raise ValueError(f"{source} contains an unpinned dependency: {dependency}")
        pins[_normalized_name(name)] = version.strip()
    return pins


def shared_pin_errors(
    project: dict[str, str],
    runtime: dict[str, str],
    legacy: dict[str, str],
    blackwell: dict[str, str],
    test: dict[str, str],
) -> list[str]:
    """Return direct-dependency drift across manifests and hardware locks."""
    errors: list[str] = []
    for package, expected in project.items():
        if runtime.get(package) != expected:
            errors.append(
                f"requirements.txt and pyproject.toml disagree on {package}"
            )
        if package == "torch":
            continue
        for scope, pins in (("legacy", legacy), ("5090", blackwell)):
            if pins.get(package) != expected:
                errors.append(
                    f"{scope} lock and pyproject.toml disagree on {package}"
                )
    for package in sorted(project.keys() & test.keys()):
        if test[package] != project[package]:
            errors.append(
                f"requirements-test.txt and pyproject.toml disagree on {package}"
            )
    return errors


def validate_dependabot_policy(path: str | Path) -> dict:
    """Check migration exclusions within the pip update configuration."""
    source = Path(path)
    if not source.exists():
        return {
            "valid": False,
            "ignored_dependencies": [],
            "errors": [f"Dependabot configuration is missing: {source}"],
        }
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
        updates = [
            item for item in payload["updates"]
            if item.get("package-ecosystem") == "pip"
        ]
        if len(updates) != 1:
            raise ValueError("expected exactly one pip update configuration")
        update = updates[0]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError) as exc:
        return {"valid": False, "ignored_dependencies": [], "errors": [str(exc)]}
    ignored = {
        _normalized_name(item["dependency-name"])
        for item in update.get("ignore", [])
        if isinstance(item, dict) and isinstance(item.get("dependency-name"), str)
        and not item.get("versions") and not item.get("update-types")
    }
    errors: list[str] = []
    routine = (update.get("groups") or {}).get("routine-dependencies") or {}
    if routine.get("group-by") != "dependency-name":
        errors.append(
            "Dependabot routine updates must be grouped by dependency name"
        )
    missing = sorted(MIGRATION_GATED_DEPENDENCIES - ignored)
    if missing:
        errors.append(
            "Dependabot must ignore migration-gated dependencies: "
            + ", ".join(missing)
        )
    return {
        "valid": not errors,
        "ignored_dependencies": sorted(ignored),
        "errors": errors,
    }


def validate_security_policy(payload: dict, today: date) -> dict:
    """Require a deadline for exceptions; an exception-free audit has no expiry."""
    if not isinstance(payload, dict):
        raise ValueError("dependency security policy must be an object")
    errors: list[str] = []
    allowances = payload.get("temporarily_allowed_vulnerabilities", {})
    if not isinstance(allowances, dict):
        errors.append("dependency exceptions must be a package-to-advisory mapping")
        allowances = {}
    if payload.get("temporarily_allowed_packages"):
        errors.append("broad package-wide vulnerability exceptions are not permitted")
    if any(
        not isinstance(package, str) or not package or "*" in package
        or not isinstance(identifiers, list) or not identifiers
        or any(not isinstance(identifier, str) or not identifier or "*" in identifier
               for identifier in identifiers)
        for package, identifiers in allowances.items()
    ):
        errors.append("dependency exceptions must list exact vulnerability IDs")
    if payload.get("status") == "no_exceptions" and allowances:
        errors.append("no_exceptions policy cannot contain vulnerability allowances")
    if not allowances and payload.get("status") != "no_exceptions":
        errors.append("an empty exception policy must declare no_exceptions")
    review_by = None
    expired = False
    if allowances:
        try:
            review_by = date.fromisoformat(str(payload["review_by"]))
        except (KeyError, ValueError):
            errors.append("dependency exceptions require a valid review_by date")
        if review_by is not None:
            expired = today > review_by
            if expired:
                errors.append(f"dependency security policy expired on {review_by.isoformat()}")
    return {
        **payload,
        "expired": expired,
        "days_until_review": (review_by - today).days if review_by else None,
        "errors": errors,
    }


def _load_policy(path: Path, today: date) -> dict:
    if not path.exists():
        return {
            "present": False,
            "expired": True,
            "errors": [f"dependency security policy is missing: {path}"],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {**validate_security_policy(payload, today), "present": True}
    except (OSError, ValueError, AttributeError) as exc:
        return {"present": True, "expired": True, "errors": [str(exc)]}


def validate_dependency_profiles(
    project_root: str | Path = PROJECT_ROOT,
    today: date | None = None,
) -> dict:
    root = Path(project_root)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        project = read_project_requirements(root / "pyproject.toml")
        runtime, _runtime_options = read_requirements(root / "requirements.txt")
        legacy, legacy_options = read_requirements(root / "requirements-lock.txt")
        blackwell, blackwell_options = read_requirements(
            root / "requirements-lock-5090.txt"
        )
        test, _test_options = read_requirements(root / "requirements-test.txt")
    except (KeyError, OSError, ValueError) as exc:
        return {
            "schema_version": 1,
            "valid": False,
            "errors": [str(exc)],
            "warnings": [],
        }

    sets = {
        "project": project,
        "runtime": runtime,
        "legacy": legacy,
        "5090": blackwell,
        "test": test,
    }
    for scope, expected_scope in EXPECTED_PINS.items():
        actual_scope = project if scope == "project" else sets[scope]
        for package, expected in expected_scope.items():
            actual = actual_scope.get(package)
            if actual != expected:
                errors.append(
                    f"{scope} requires {package}=={expected}; found {actual or 'missing'}"
                )

    errors.extend(shared_pin_errors(project, runtime, legacy, blackwell, test))
    if not any(option.endswith("/cu130") for option in legacy_options):
        errors.append("requirements-lock.txt does not select the CUDA 13.0 index")
    if not any(option.endswith("/cu130") for option in blackwell_options):
        errors.append("requirements-lock-5090.txt does not select the CUDA 13.0 index")

    if legacy != blackwell:
        errors.append("GPU locks must use the same validated dependency versions")

    policy = _load_policy(
        root / ".github" / "dependency-policy.json",
        today or datetime.now(timezone.utc).date(),
    )
    errors.extend(policy.get("errors", []))
    if policy.get("temporarily_allowed_vulnerabilities") and not policy.get("errors"):
        warnings.append(
            f"Temporary advisory exceptions require review in {policy['days_until_review']} day(s)"
        )

    dependabot = validate_dependabot_policy(root / ".github" / "dependabot.yml")
    errors.extend(dependabot["errors"])

    return {
        "schema_version": 1,
        "valid": not errors,
        "profiles": {
            "3080": {
                "lock": "requirements-lock.txt",
                "torch": legacy.get("torch"),
                "cuda_index": "cu130",
            },
            "4090": {
                "lock": "requirements-lock.txt",
                "torch": legacy.get("torch"),
                "cuda_index": "cu130",
            },
            "5090": {
                "lock": "requirements-lock-5090.txt",
                "torch": blackwell.get("torch"),
                "cuda_index": "cu130",
            },
        },
        "libraries": {
            package: project.get(package) for package in EXPECTED_PINS["project"]
        },
        "security_policy": policy,
        "dependabot_policy": dependabot,
        "errors": errors,
        "warnings": warnings,
    }


def installed_dependency_status(profile: str) -> dict:
    lock_name = "requirements-lock-5090.txt" if profile == "5090" else "requirements-lock.txt"
    expected, _options = read_requirements(PROJECT_ROOT / lock_name)
    packages = tuple(sorted(expected))
    installed: dict[str, str | None] = {}
    errors = []
    for package in packages:
        try:
            installed[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            installed[package] = None
        if installed[package] != expected.get(package):
            errors.append(
                f"installed {package} is {installed[package] or 'missing'}; "
                f"{profile} requires {expected.get(package)}"
            )
    return {
        "valid": not errors,
        "profile": profile,
        "lock": lock_name,
        "installed": installed,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--profile", choices=("3080", "4090", "5090"), default="3080")
    args = parser.parse_args(argv)

    result = validate_dependency_profiles()
    if args.installed:
        result["installed"] = installed_dependency_status(args.profile)
    print(json.dumps(result, indent=2))
    invalid = not result["valid"] or not result.get("installed", {"valid": True})["valid"]
    return 1 if args.check and invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
