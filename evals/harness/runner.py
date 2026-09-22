from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.db import SourceBundle, SourceImage
from recipe_cooking_assistant.extraction import OpenAIExtractionClient
from recipe_cooking_assistant.models import ExtractionResult
from recipe_cooking_assistant.normalize import normalize_result

from evals.harness.assertions import AssertionFailure, collect_semantic_failures
from evals.harness.catalog import EvalCase, RESULTS_DIR, load_cases


@dataclass
class CaseRunResult:
    case_id: str
    ok: bool
    error: str | None = None
    failures: list[str] | None = None
    usage: dict[str, Any] | None = None
    latency_ms: float | None = None


def run_deterministic_case(case: EvalCase) -> CaseRunResult:
    raw = case.mock_output
    result = normalize_result(ExtractionResult.model_validate(raw), [])
    failures = collect_semantic_failures(case.id, result, case.expect)
    if not failures:
        return CaseRunResult(case_id=case.id, ok=True, failures=[])
    return CaseRunResult(
        case_id=case.id,
        ok=False,
        error=str(AssertionFailure(case.id, failures)),
        failures=failures,
    )


def run_deterministic(ids: list[str] | None = None) -> list[CaseRunResult]:
    return [run_deterministic_case(case) for case in load_cases(ids=ids)]


def plan_live(
    ids: list[str] | None = None,
    *,
    limit: int | None = None,
) -> tuple[list[EvalCase], int, float]:
    cases = [c for c in load_cases(ids=ids) if c.live_enabled]
    if limit is not None:
        cases = cases[:limit]
    api_calls = len(cases)  # one extraction call per case
    max_cost = sum(c.estimated_max_cost_usd for c in cases)
    return cases, api_calls, max_cost


def _bundle_for_case(case: EvalCase, settings: Settings) -> SourceBundle:
    images: list[SourceImage] = []
    for index, path in enumerate(case.live_image_paths()):
        if not path.is_file():
            raise FileNotFoundError(
                f"Live image missing for {case.id}: {path}. "
                "Place files under evals/local/ (gitignored) for manual runs."
            )
        images.append(
            SourceImage(
                id=str(uuid4()),
                bundle_id="eval",
                session_id="eval",
                filename=path.name,
                stored_path=str(path),
                content_type="image/png"
                if path.suffix.lower() == ".png"
                else "image/jpeg",
                size_bytes=path.stat().st_size,
                sort_index=index,
                created_at="eval",
            )
        )
    return SourceBundle(
        id=str(uuid4()),
        session_id="eval",
        raw_text=case.source_text,
        created_at="eval",
        expires_at="eval",
        images=images,
    )


def run_live_case(
    case: EvalCase,
    *,
    settings: Settings,
    client: OpenAIExtractionClient,
) -> tuple[CaseRunResult, ExtractionResult | None]:
    bundle = _bundle_for_case(case, settings)
    started = time.perf_counter()
    result, usage = client.extract(bundle=bundle, settings=settings)
    latency_ms = (time.perf_counter() - started) * 1000
    failures = collect_semantic_failures(case.id, result, case.expect)
    ok = not failures
    error = None if ok else str(AssertionFailure(case.id, failures))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "case_id": case.id,
        "ok": ok,
        "error": error,
        "assertion_failures": failures,
        "model": usage.model,
        "latency_ms": round(latency_ms, 1),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "estimated_cost_usd": usage.estimated_cost_usd,
        "result": result.model_dump(),
    }
    (RESULTS_DIR / f"{case.id}.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8"
    )
    return (
        CaseRunResult(
            case_id=case.id,
            ok=ok,
            error=error,
            failures=failures,
            usage={
                "model": usage.model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "estimated_cost_usd": usage.estimated_cost_usd,
            },
            latency_ms=latency_ms,
        ),
        result,
    )
