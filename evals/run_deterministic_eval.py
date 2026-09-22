"""Deterministic evaluation entrypoint (no API calls)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python evals/run_deterministic_eval.py` from repo root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from evals.harness.runner import run_deterministic  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic extraction evals")
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        help="Case id to run (repeatable). Default: all cases.",
    )
    args = parser.parse_args()
    results = run_deterministic(ids=args.cases)
    failed = [r for r in results if not r.ok]
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"{status}  {result.case_id}")
        if result.error:
            print(f"       {result.error}")
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
