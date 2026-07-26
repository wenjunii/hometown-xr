"""Validate dependency locks shared by the three GPU workstation profiles."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from config import PROJECT_ROOT

EXPECTED_PINS = {
    "project": {
        "sentence-transformers": "2.7.0",
        "torch": "2.1.0",
        "transformers": "4.40.2",
    },
    "legacy": {
        "sentence-transformers": "2.7.0",
        "torch": "2.1.0+cu121",
        "transformers": "4.40.2",
    },
    "5090": {
        "sentence-transformers": "2.7.0",
        "torch": "2.12.1+cu130",
        "transformers": "4.40.2",
    },
}


def _normalized_name(value: str) -> str:
    return value.strip().lower().replace("_", "-")


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


def _load_policy(path: Path, today: date) -> dict:
    if not path.exists():
        return {
            "present": False,
            "expired": True,
            "errors": [f"dependency security policy is missing: {path}"],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    review_by = date.fromisoformat(str(payload["review_by"]))
    return {
        **payload,
        "present": True,
        "expired": today > review_by,
        "days_until_review": (review_by - today).days,
        "errors": (
            [f"dependency security policy expired on {review_by.isoformat()}"]
            if today > review_by
            else []
        ),
    }


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
    }
    for scope, expected_scope in EXPECTED_PINS.items():
        actual_scope = project if scope == "project" else sets[scope]
        for package, expected in expected_scope.items():
            actual = actual_scope.get(package)
            if actual != expected:
                errors.append(
                    f"{scope} requires {package}=={expected}; found {actual or 'missing'}"
                )

    for package in EXPECTED_PINS["project"]:
        if runtime.get(package) != project.get(package):
            errors.append(
                f"requirements.txt and pyproject.toml disagree on {package}"
            )
    if not any(option.endswith("/cu121") for option in legacy_options):
        errors.append("requirements-lock.txt does not select the CUDA 12.1 index")
    if not any(option.endswith("/cu130") for option in blackwell_options):
        errors.append("requirements-lock-5090.txt does not select the CUDA 13.0 index")

    transformers_major = int(project["transformers"].split(".", maxsplit=1)[0])
    if transformers_major >= 5:
        errors.append(
            "sentence-transformers 2.7.0 requires Transformers below major version 5"
        )

    policy = _load_policy(
        root / ".github" / "dependency-policy.json",
        today or datetime.now(timezone.utc).date(),
    )
    errors.extend(policy.get("errors", []))
    if policy.get("status") == "migration_required" and not policy.get("expired"):
        allowlist = policy.get("temporarily_allowed_vulnerabilities")
        if not isinstance(allowlist, dict) or not allowlist:
            errors.append(
                "dependency policy must list exact temporarily allowed vulnerability IDs"
            )
        elif any(
            not isinstance(identifiers, list) or not identifiers
            for identifiers in allowlist.values()
        ):
            errors.append(
                "each temporary dependency exception must contain vulnerability IDs"
            )
        if policy.get("temporarily_allowed_packages"):
            errors.append(
                "broad package-wide vulnerability exceptions are not permitted"
            )
        warnings.append(
            "Torch and Transformers have exact, temporary advisory exceptions; "
            f"review in {policy['days_until_review']} day(s)"
        )

    return {
        "schema_version": 1,
        "valid": not errors,
        "profiles": {
            "3080": {
                "lock": "requirements-lock.txt",
                "torch": legacy.get("torch"),
                "cuda_index": "cu121",
            },
            "4090": {
                "lock": "requirements-lock.txt",
                "torch": legacy.get("torch"),
                "cuda_index": "cu121",
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
        "errors": errors,
        "warnings": warnings,
    }


def installed_dependency_status(profile: str) -> dict:
    lock_name = "requirements-lock-5090.txt" if profile == "5090" else "requirements-lock.txt"
    expected, _options = read_requirements(PROJECT_ROOT / lock_name)
    packages = ("sentence-transformers", "torch", "transformers")
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
