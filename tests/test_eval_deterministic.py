from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.harness.catalog import load_cases
from evals.harness.runner import run_deterministic


def test_all_deterministic_eval_cases_pass() -> None:
    results = run_deterministic()
    assert results, "expected eval cases"
    failures = [r for r in results if not r.ok]
    assert not failures, "\n".join(f"{r.case_id}: {r.error}" for r in failures)


def test_eval_catalog_covers_required_themes() -> None:
    ids = {c.id for c in load_cases()}
    required = {
        "CHILI_LOCAL_SCREENSHOTS",
        "T1_plain_text",
        "M1_interleaved_boilerplate",
        "CREAMY_TOMATO_INSTRUCTION_ONLY",
        "ALT_PROTEIN_OR",
        "PACKAGE_CANNED",
        "OPTIONAL_GARNISH",
        "CONTRADICTION_QUANTITY",
        "I1_DISH_ONLY",
        "MULTI_SCREENSHOT_ORDER",
        "OVERLAP_SCREENSHOTS",
        "UNCERTAIN_ORDER",
        "MISSING_QUANTITY",
        "QUALIFIER_TO_TASTE",
        "SERVINGS_NORMALIZE",
        "ATOMIC_STEPS_EVIDENCE",
    }
    missing = required - ids
    assert not missing, f"missing cases: {missing}"


def test_live_runner_is_not_imported_by_pytest_collection() -> None:
    # Guardrail: live module must remain an explicit CLI entrypoint.
    live_path = ROOT / "evals" / "run_live_eval.py"
    assert live_path.is_file()
    text = live_path.read_text(encoding="utf-8")
    assert "--execute" in text
    assert "Never imported by pytest" in text or "manual only" in text.lower()
