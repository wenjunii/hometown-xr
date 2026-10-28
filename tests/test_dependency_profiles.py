import json
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

import dependency_profiles
from dependency_profiles import (
    EXPECTED_PINS,
    MIGRATION_GATED_DEPENDENCIES,
    read_project_requirements,
    read_requirements,
    shared_pin_errors,
    validate_dependabot_policy,
    validate_dependency_profiles,
    validate_security_policy,
)


def test_torch_pins_match_each_cuda_profile():
    project = read_project_requirements("pyproject.toml")
    runtime, _ = read_requirements("requirements.txt")
    legacy, legacy_options = read_requirements("requirements-lock.txt")
    blackwell, blackwell_options = read_requirements("requirements-lock-5090.txt")

    assert project["torch"] == runtime["torch"] == "2.1.0"
    assert legacy["torch"] == "2.1.0+cu121"
    assert blackwell["torch"] == "2.12.1+cu130"
    assert any(option.endswith("/cu121") for option in legacy_options)
    assert any(option.endswith("/cu130") for option in blackwell_options)


def test_transformers_stays_compatible_across_profiles():
    project = read_project_requirements("pyproject.toml")
    runtime, _ = read_requirements("requirements.txt")
    legacy, _ = read_requirements("requirements-lock.txt")
    blackwell, _ = read_requirements("requirements-lock-5090.txt")

    versions = {
        project["transformers"],
        runtime["transformers"],
        legacy["transformers"],
        blackwell["transformers"],
    }
    assert versions == {"4.40.2"}
    assert int(next(iter(versions)).split(".", maxsplit=1)[0]) < 5


def test_dependency_policy_is_current_and_profiles_are_valid():
    result = validate_dependency_profiles(today=date(2026, 10, 28))

    assert result["valid"]
    assert result["security_policy"]["status"] == "migration_required"
    assert result["warnings"]
    assert result["libraries"] == EXPECTED_PINS["project"]


def test_dependabot_keeps_result_sensitive_packages_migration_gated():
    result = validate_dependabot_policy(".github/dependabot.yml")

    assert result["valid"]
    assert MIGRATION_GATED_DEPENDENCIES <= set(result["ignored_dependencies"])


def test_shared_pin_validation_rejects_manifest_drift():
    project = {"numpy": "1.26.4", "torch": "2.1.0"}
    runtime = {"numpy": "2.2.6", "torch": "2.1.0"}
    legacy = {"numpy": "1.26.4", "torch": "2.1.0+cu121"}
    blackwell = {"numpy": "1.26.4", "torch": "2.12.1+cu130"}
    test = {"numpy": "1.26.4"}

    errors = shared_pin_errors(project, runtime, legacy, blackwell, test)

    assert errors == ["requirements.txt and pyproject.toml disagree on numpy"]


@pytest.mark.parametrize("change", [
    {"reviewed_on": "2026-10-01"},
    {"reviewed_on": "2026-10-29"},
    {"review_by": "2027-01-01"},
    {"review_by": "invalid"},
    {"exception_versions": {}},
    {"exception_versions": {"torch": ["*"]}},
    {"temporarily_allowed_packages": ["torch"]},
    {"temporarily_allowed_vulnerabilities": {"pillow": ["EXACT-ID"]}},
    {"temporarily_allowed_vulnerabilities": {"torch": ["*"]}},
    {"status": "no_exceptions"},
    {"schema_version": 2},
])
def test_model_exception_policy_rejects_widened_or_malformed_scope(change):
    policy = json.loads(Path(".github/dependency-policy.json").read_text(encoding="utf-8"))
    policy.update(deepcopy(change))
    assert validate_security_policy(policy, date(2026, 10, 28))["errors"]


def test_model_exception_expires_without_automatic_renewal():
    result = validate_dependency_profiles(today=date(2026, 11, 12))
    assert not result["valid"]
    assert result["security_policy"]["expired"]


def test_installed_check_includes_patched_transitive_packages(monkeypatch):
    pins, _ = read_requirements("requirements-lock.txt")
    monkeypatch.setattr(
        dependency_profiles.importlib.metadata, "version",
        lambda name: "3.29.0" if name == "filelock" else pins[name],
    )
    result = dependency_profiles.installed_dependency_status("3080")
    assert not result["valid"]
    assert len(result["installed"]) == len(pins)
    assert result["errors"] == ["installed filelock is 3.29.0; 3080 requires 4.2.1"]


def test_5090_setuptools_constraint_is_explicit_and_not_excepted():
    legacy, _ = read_requirements("requirements-lock.txt")
    blackwell, _ = read_requirements("requirements-lock-5090.txt")
    assert legacy["setuptools"] == "84.2.1"
    assert blackwell["setuptools"] == "81.0.0"
    policy = json.loads(Path(".github/dependency-policy.json").read_text(encoding="utf-8"))
    assert "setuptools" not in policy["temporarily_allowed_vulnerabilities"]
