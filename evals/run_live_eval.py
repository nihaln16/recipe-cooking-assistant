"""Explicit live-model evaluation. Never imported by pytest/CI.

Usage:
  uv run python evals/run_live_eval.py --plan
  uv run python evals/run_live_eval.py --case CREAMY_TOMATO --execute
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from recipe_cooking_assistant.config import Settings  # noqa: E402
from recipe_cooking_assistant.extraction import (  # noqa: E402
    OpenAIExtractionClient,
    get_extraction_client,
)

from evals.harness.runner import plan_live, run_live_case  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live OpenAI Responses API evaluation (manual only)"
    )
    parser.add_argument("--case", action="append", dest="cases", help="Case id(s)")
    parser.add_argument("--limit", type=int, default=None, help="Max cases to run")
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Show selected cases, API call count, and max cost; do not call the API",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually call the API (requires OPENAI_API_KEY). Implies confirmation via flag.",
    )
    args = parser.parse_args()

    cases, api_calls, max_cost = plan_live(ids=args.cases, limit=args.limit)
    print("Live evaluation plan")
    print(f"  Cases ({len(cases)}):")
    for case in cases:
        imgs = len(case.live_image_paths())
        print(
            f"    - {case.id}  (images={imgs}, "
            f"est_max=${case.estimated_max_cost_usd:.4f})"
        )
    print(f"  Expected API calls: {api_calls}")
    print(f"  Estimated maximum cost: ${max_cost:.4f}")
    print("  API: OpenAI Responses (/v1/responses) only")

    if args.plan and not args.execute:
        print("\nPlan only — no API calls made.")
        return 0

    if not args.execute:
        print("\nRefusing to run: pass --execute after reviewing the plan.")
        print("Example: uv run python evals/run_live_eval.py --plan")
        print("Then:    uv run python evals/run_live_eval.py --case ID --execute")
        return 2

    if not cases:
        print("No live-enabled cases selected.")
        return 1

    settings = Settings()
    client = get_extraction_client(settings)
    assert isinstance(client, OpenAIExtractionClient)

    failures = 0
    for case in cases:
        print(f"\nRunning {case.id} ...")
        result, _ = run_live_case(case, settings=settings, client=client)
        status = "PASS" if result.ok else "FAIL"
        print(f"  {status}  latency_ms={result.latency_ms:.0f} usage={result.usage}")
        if result.error:
            print(f"  {result.error}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
