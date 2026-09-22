"""Re-run semantic assertions on saved live results without calling the API."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from recipe_cooking_assistant.models import ExtractionResult  # noqa: E402
from recipe_cooking_assistant.normalize import normalize_result  # noqa: E402

from evals.harness.assertions import (  # noqa: E402
    AssertionFailure,
    collect_semantic_failures,
)
from evals.harness.catalog import RESULTS_DIR, load_cases  # noqa: E402


def recheck_case(case_id: str) -> int:
    cases = {c.id: c for c in load_cases()}
    if case_id not in cases:
        print(f"Unknown case id: {case_id}")
        return 2
    path = RESULTS_DIR / f"{case_id}.json"
    if not path.is_file():
        print(f"No saved result at {path}")
        return 2

    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("result")
    if raw is None:
        print(f"Saved file missing result payload: {path}")
        return 2

    result = normalize_result(ExtractionResult.model_validate(raw), [])
    failures = collect_semantic_failures(case_id, result, cases[case_id].expect)

    # Update ok/error on the saved file without changing model usage fields.
    payload["ok"] = not failures
    payload["error"] = (
        None
        if not failures
        else str(AssertionFailure(case_id, failures))
    )
    payload["assertion_failures"] = failures
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"Rechecked {case_id} from {path} (no API call)")
    if not failures:
        print("PASS — no semantic failures")
        return 0

    print(f"FAIL — {len(failures)} semantic failure(s):")
    for index, message in enumerate(failures, start=1):
        print(f"  {index}. {message}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-score saved live eval JSON without API calls"
    )
    parser.add_argument("case_id", help="Case id, e.g. CHILI_LOCAL_SCREENSHOTS")
    args = parser.parse_args()
    return recheck_case(args.case_id)


if __name__ == "__main__":
    raise SystemExit(main())
