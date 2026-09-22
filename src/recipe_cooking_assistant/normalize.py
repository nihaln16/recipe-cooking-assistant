"""Post-extraction normalization helpers."""

from __future__ import annotations

import re
from typing import Any

from recipe_cooking_assistant.db import SourceImage
from recipe_cooking_assistant.ingredient_display import normalize_ingredient_qualifiers
from recipe_cooking_assistant.models import (
    ExtractionFinding,
    ExtractionResult,
    ReviewFlag,
)
from recipe_cooking_assistant.quantity_consistency import (
    apply_quantity_consistency_checks,
)

_SERVINGS_RE = re.compile(
    r"(?i)^\s*(?:serves?|servings?|yield(?:s)?|makes?)?\s*:?\s*"
    r"(?P<num>\d+(?:\.\d+)?)\s*(?:servings?|people|portions?)?\s*$"
)
_SERVINGS_EMBEDDED_RE = re.compile(
    r"(?i)(?:serves?|servings?|yield(?:s)?|makes?)\s*:?\s*(?P<num>\d+(?:\.\d+)?)"
)


def normalize_servings(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split()).strip()
    if not text:
        return None
    match = _SERVINGS_RE.match(text)
    if match:
        return match.group("num")
    embedded = _SERVINGS_EMBEDDED_RE.search(text)
    if embedded:
        return embedded.group("num")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return text
    return text


def _finding_key(finding: ExtractionFinding) -> tuple[str, str, tuple[str, ...]]:
    return (finding.type, finding.message.lower(), tuple(finding.related_ids))


def ensure_instruction_only_findings(result: ExtractionResult) -> None:
    existing = {_finding_key(f) for f in result.findings}
    for ingredient in result.instruction_only_ingredients():
        message = "Referenced in instructions but absent from ingredient list"
        finding = ExtractionFinding(
            id=f"find_{ingredient.id}",
            type="instruction_only_ingredient",
            message=message,
            related_ids=[ingredient.id],
            evidence=ingredient.evidence,
        )
        if _finding_key(finding) in existing:
            continue
        result.findings.append(finding)
        existing.add(_finding_key(finding))
        # Ready for milestone 3 review UI; also mirror in review_flags.
        if not any(
            f.type == "instruction_only_ingredient" and ingredient.id in f.related_ids
            for f in result.review_flags
        ):
            result.review_flags.append(
                ReviewFlag(
                    type="instruction_only_ingredient",
                    message=message,
                    related_ids=[ingredient.id],
                )
            )


def sync_review_flags_into_findings(result: ExtractionResult) -> None:
    existing = {_finding_key(f) for f in result.findings}
    for index, flag in enumerate(result.review_flags):
        finding = ExtractionFinding(
            id=f"flag_{index}_{flag.type}",
            type=flag.type,
            message=flag.message,
            related_ids=list(flag.related_ids),
            evidence=None,
        )
        key = _finding_key(finding)
        if key in existing:
            continue
        result.findings.append(finding)
        existing.add(key)


def normalize_result(
    result: ExtractionResult, images: list[SourceImage]
) -> ExtractionResult:
    """Normalize evidence, qualifiers, servings, and findings."""
    by_index = {img.sort_index: img for img in images}

    def fix_evidence(item: Any) -> None:
        ev = getattr(item, "evidence", None)
        if ev is None:
            return
        if ev.image_id is None and ev.image_index is not None:
            match = by_index.get(ev.image_index)
            if match:
                ev.image_id = match.id
        if getattr(item, "confidence", None) == "uncertain" and getattr(
            item, "provenance", None
        ) == "source":
            item.provenance = "needs_review"

    result.servings = normalize_servings(result.servings)

    for ingredient in result.ingredients:
        fix_evidence(ingredient)
        normalize_ingredient_qualifiers(ingredient)
        if ingredient.list_status == "instruction_only":
            # Do not treat step mentions as settled list amounts.
            ingredient.quantity = None
            ingredient.unit = None
            if ingredient.provenance == "source":
                ingredient.provenance = "needs_review"

    for step in result.steps:
        fix_evidence(step)
        if not step.source_direction_text and step.evidence and step.evidence.quote:
            # Keep evidence quote; source_direction_text may equal atomic text for unsplit steps.
            if step.text.strip() != (step.evidence.quote or "").strip():
                step.source_direction_text = step.evidence.quote

    for note in result.creator_notes:
        fix_evidence(note)

    for finding in result.findings:
        ev = finding.evidence
        if ev is None:
            continue
        if ev.image_id is None and ev.image_index is not None:
            match = by_index.get(ev.image_index)
            if match:
                ev.image_id = match.id

    if result.insufficient_source:
        result.ingredients = []
        result.steps = []
        return result

    if not result.ingredients and not result.steps:
        result.insufficient_source = True
        result.insufficient_reason = (
            result.insufficient_reason
            or "No readable ingredients or steps were found in the provided source."
        )
        return result

    ensure_instruction_only_findings(result)
    apply_quantity_consistency_checks(result)
    sync_review_flags_into_findings(result)
    return result
