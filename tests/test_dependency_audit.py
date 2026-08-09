from datetime import date

from dependency_audit import evaluate_audit_report

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
