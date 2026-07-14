"""Evaluate the lightweight filters against the checked-in labeled cases."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from config import MIN_NARRATIVE_INDICATORS
    from matcher import NarrativeFilter

    fixture = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "filter_cases.json"
    cases = json.loads(fixture.read_text(encoding="utf-8"))
    narrative_filter = NarrativeFilter()
    correct = 0
    for case in cases:
        actual = narrative_filter.passes(case["text"], MIN_NARRATIVE_INDICATORS)
        correct += actual == case["expected"]
        print(
            f"{'PASS' if actual == case['expected'] else 'FAIL'} "
            f"{case['id']}: expected={case['expected']} actual={actual}"
        )
    print(f"Accuracy: {correct}/{len(cases)}")
    return 0 if correct == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
