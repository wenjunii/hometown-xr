from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def _requirements(path: str) -> tuple[dict[str, str], list[str]]:
    pins: dict[str, str] = {}
    options: list[str] = []
    for raw_line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--"):
            options.append(line)
            continue
        if line.startswith("-r "):
            continue
        name, separator, version = line.partition("==")
        assert separator, f"{path} contains an unpinned requirement: {line}"
        pins[name.lower().replace("_", "-")] = version
    return pins, options


def _project_requirements() -> dict[str, str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    pins: dict[str, str] = {}
    for dependency in dependencies:
        name, separator, version = dependency.partition("==")
        assert separator, f"pyproject.toml contains an unpinned dependency: {dependency}"
        pins[name.lower().replace("_", "-")] = version
    return pins


def test_torch_pins_match_each_cuda_profile():
    project = _project_requirements()
    runtime, _ = _requirements("requirements.txt")
    legacy, legacy_options = _requirements("requirements-lock.txt")
    blackwell, blackwell_options = _requirements("requirements-lock-5090.txt")

    assert project["torch"] == runtime["torch"] == "2.1.0"
    assert legacy["torch"] == "2.1.0+cu121"
    assert blackwell["torch"] == "2.12.1+cu130"
    assert any(option.endswith("/cu121") for option in legacy_options)
    assert any(option.endswith("/cu130") for option in blackwell_options)


def test_transformers_stays_compatible_across_profiles():
    project = _project_requirements()
    runtime, _ = _requirements("requirements.txt")
    legacy, _ = _requirements("requirements-lock.txt")
    blackwell, _ = _requirements("requirements-lock-5090.txt")

    versions = {
        project["transformers"],
        runtime["transformers"],
        legacy["transformers"],
        blackwell["transformers"],
    }
    assert versions == {"4.40.2"}
    assert int(next(iter(versions)).split(".", maxsplit=1)[0]) < 5
