from datetime import date

from dependency_profiles import (
    EXPECTED_PINS,
    MIGRATION_GATED_DEPENDENCIES,
    read_project_requirements,
    read_requirements,
    shared_pin_errors,
    validate_dependabot_policy,
    validate_dependency_profiles,
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
    result = validate_dependency_profiles(today=date(2026, 7, 21))

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
