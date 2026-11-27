from datetime import date

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

    assert project["torch"] == runtime["torch"] == "2.13.0"
    assert legacy == blackwell
    assert blackwell["torch"] == "2.13.0+cu130"
    assert any(option.endswith("/cu130") for option in legacy_options)
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
    assert versions == {"5.10.4"}


def test_dependency_policy_is_current_and_profiles_are_valid():
    result = validate_dependency_profiles(today=date(2026, 7, 21))

    assert result["valid"]
    assert result["security_policy"]["status"] == "no_exceptions"
    assert not result["warnings"]
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


def test_exception_free_policy_does_not_expire():
    result = validate_security_policy(
        {"status": "no_exceptions", "temporarily_allowed_vulnerabilities": {}},
        today=date(2030, 1, 1),
    )
    assert not result["errors"]
    assert result["days_until_review"] is None


def test_exceptions_still_require_deadline_and_exact_ids():
    result = validate_security_policy(
        {"temporarily_allowed_vulnerabilities": {"torch": ["*"]}},
        today=date(2026, 10, 28),
    )
    assert len(result["errors"]) == 2
    result = validate_security_policy(
        {"review_by": "2026-08-31", "temporarily_allowed_vulnerabilities": {"torch": ["CVE-1"]}},
        today=date(2026, 10, 28),
    )
    assert result["expired"]
    assert result["errors"]


def test_dependabot_policy_does_not_accept_comments_or_version_limited_ignores(tmp_path):
    import yaml

    source = tmp_path / "dependabot.yml"
    payload = {"updates": [{
        "package-ecosystem": "pip",
        "groups": {"routine-dependencies": {"group-by": "dependency-name"}},
        "ignore": [
            {"dependency-name": package, "versions": ["<1"]}
            for package in MIGRATION_GATED_DEPENDENCIES
        ],
    }]}
    source.write_text(yaml.safe_dump(payload) + "\n# - dependency-name: torch\n", encoding="utf-8")
    assert not validate_dependabot_policy(source)["valid"]


def test_installed_profile_checks_transitive_security_pins(monkeypatch):
    monkeypatch.setattr(dependency_profiles, "read_requirements", lambda path: (
        {"torch": "2.13.0+cu130", "pillow": "13.0.0"}, [],
    ))
    monkeypatch.setattr(dependency_profiles.importlib.metadata, "version", lambda name: {
        "torch": "2.13.0+cu130", "pillow": "12.3.0",
    }[name])
    result = dependency_profiles.installed_dependency_status("3080")
    assert not result["valid"]
    assert result["errors"] == ["installed pillow is 12.3.0; 3080 requires 13.0.0"]
