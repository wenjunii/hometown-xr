import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import dependency_audit
from dependency_audit import audit_pins, evaluate_audit_report, run_pip_audit

POLICY = {
    "review_by": "2026-08-31",
    "temporarily_allowed_vulnerabilities": {
        "torch": ["TEST-1"],
        "transformers": ["TEST-2"],
    },
}


def test_known_model_stack_findings_are_allowed_before_review_date():
    report = {
        "dependencies": [
            {"name": "torch", "version": "2.1.0", "vulns": [{"id": "TEST-1"}]},
            {
                "name": "transformers",
                "version": "4.40.2",
                "vulns": [{"id": "TEST-2"}],
            },
        ]
    }

    result = evaluate_audit_report(report, POLICY, today=date(2026, 7, 21))

    assert result["valid"]
    assert not result["unexpected_packages"]
    assert not result["unexpected_vulnerabilities"]


def test_new_advisory_for_allowed_package_fails():
    report = {
        "dependencies": [
            {
                "name": "torch",
                "version": "2.1.0",
                "vulns": [{"id": "TEST-1"}, {"id": "TEST-NEW"}],
            }
        ]
    }

    result = evaluate_audit_report(report, POLICY, today=date(2026, 7, 21))

    assert not result["valid"]
    assert result["unexpected_vulnerabilities"][0]["vulnerability_ids"] == [
        "TEST-NEW"
    ]
    assert "torch:TEST-NEW" in result["errors"][0]


def test_new_vulnerable_package_or_expired_policy_fails():
    report = {
        "dependencies": [
            {"name": "requests", "version": "1.0", "vulns": [{"id": "TEST-3"}]}
        ]
    }

    result = evaluate_audit_report(report, POLICY, today=date(2026, 9, 1))

    assert not result["valid"]
    assert len(result["errors"]) == 2
    assert result["unexpected_packages"][0]["package"] == "requests"


def test_no_exceptions_audit_passes_without_expiry():
    report = {"dependencies": [{"name": "pillow", "version": "13.0.0", "vulns": []}]}
    result = evaluate_audit_report(report, {"status": "no_exceptions"}, today=date(2030, 1, 1))
    assert result["valid"]
    assert result["review_by"] is None


@pytest.mark.parametrize("entry", [
    {"name": "torch", "skip_reason": "version not found"},
    {"name": "torch", "version": "2.13.0"},
    {"name": "torch", "version": "2.13.0", "vulns": [{}]},
    {"name": "pillow", "version": "13.0.0", "vulns": [{"id": "NEW-ADVISORY"}]},
])
def test_skipped_incomplete_and_new_findings_fail(entry):
    assert not evaluate_audit_report(
        {"dependencies": [entry]}, {"status": "no_exceptions"}
    )["valid"]


def test_audit_normalizes_cuda_and_includes_transitive_and_nested_pins(tmp_path):
    nested = tmp_path / "nested.txt"
    nested.write_text("pillow==13.0.0\n", encoding="utf-8")
    source = tmp_path / "requirements.txt"
    source.write_text(
        "--extra-index-url https://download.pytorch.org/whl/cu130\n"
        "-r nested.txt\ntorch==2.13.0+cu130\n"
        "Some_Package==1.2; sys_platform == 'linux'\n",
        encoding="utf-8",
    )
    assert audit_pins(source) == {"pillow": "13.0.0", "torch": "2.13.0", "some-package": "1.2"}


@pytest.mark.parametrize("text", [
    "pillow>=13\n", "pillow==13.*\n", "pillow @ https://example.org/pillow.whl\n",
    "-r requirements.txt\n", "pillow==12\npillow==13\n", "pillow==13+custom\n",
])
def test_audit_rejects_unpinned_cyclic_or_conflicting_requirements(tmp_path, text):
    source = tmp_path / "requirements.txt"
    source.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        audit_pins(source)


@pytest.mark.parametrize(("returncode", "dependencies"), [
    (0, []),
    (0, [{"name": "torch", "version": "2.1.0", "vulns": []}]),
    (1, [{"name": "torch", "version": "2.13.0", "vulns": []}]),
    (2, [{"name": "torch", "version": "2.13.0", "vulns": []}]),
])
def test_audit_process_errors_and_missing_coverage_fail(tmp_path, monkeypatch, returncode, dependencies):
    source = tmp_path / "requirements.txt"
    source.write_text("torch==2.13.0+cu130\n", encoding="utf-8")

    def fake_run(args, **kwargs):
        assert "--disable-pip" in args
        assert kwargs["timeout"] == 300
        assert Path(args[args.index("-r") + 1]).read_text() == "torch==2.13.0\n"
        output = Path(args[args.index("--output") + 1])
        output.write_text(json.dumps({"dependencies": dependencies}), encoding="utf-8")
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(dependency_audit.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        run_pip_audit(source)


def test_cli_defaults_to_all_manifests_and_bad_policy_fails(tmp_path, monkeypatch, capsys):
    seen = []
    policy = tmp_path / "policy.json"
    policy.write_text('{"status": "no_exceptions"}', encoding="utf-8")

    def fake_audit(path):
        seen.append(path.name)
        return {"dependencies": [{"name": "pillow", "version": "13.0.0", "vulns": []}]}

    monkeypatch.setattr(dependency_audit, "run_pip_audit", fake_audit)
    assert dependency_audit.main(["--policy", str(policy)]) == 0
    assert tuple(seen) == dependency_audit.DEFAULT_MANIFESTS
    policy.write_text("[]", encoding="utf-8")
    assert dependency_audit.main(["--policy", str(policy)]) == 1
    assert "must be an object" in capsys.readouterr().out


def test_compatibility_launchers_resolve_to_audited_gpu_locks():
    assert audit_pins(Path("4090/requirements.txt")) == audit_pins(Path("requirements-lock.txt"))
    assert audit_pins(Path("5090/requirements.txt")) == audit_pins(Path("requirements-lock-5090.txt"))
