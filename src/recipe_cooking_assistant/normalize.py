"""Post-extraction normalization helpers."""

from __future__ import annotations

import re
from typing import Any

from recipe_cooking_assistant.db import SourceImage
from recipe_cooking_assistant.ingredient_display import (
    is_qualifier_phrase,
    normalize_ingredient_qualifiers,
)
from recipe_cooking_assistant.models import (
    ExtractedIngredient,
    ExtractionFinding,
    ExtractionResult,
    ReviewDecision,
    ReviewFlag,
    SourceEvidence,
)
from recipe_cooking_assistant.quantity_consistency import (
    apply_quantity_consistency_checks,
    source_amount_for_instruction_ingredient,
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


def _norm_finding_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.lower().split()).rstrip(".,;:!")


def semantic_finding_key(finding: ExtractionFinding) -> tuple:
    """Stable identity for one review-queue item: type + target, not generated ids."""
    related = tuple(sorted(finding.related_ids))
    if related:
        return (finding.type, related)
    evidence = finding.evidence
    quote = _norm_finding_text(evidence.quote if evidence else None)
    image = ""
    if evidence:
        if evidence.image_id:
            image = evidence.image_id
        elif evidence.image_index is not None:
            image = f"idx:{evidence.image_index}"
    return (finding.type, related, _norm_finding_text(finding.message), quote, image)


def _finding_key(finding: ExtractionFinding) -> tuple:
    return semantic_finding_key(finding)


def _flag_key(flag: ReviewFlag) -> tuple:
    related = tuple(sorted(flag.related_ids))
    if related:
        return (flag.type, related)
    return (flag.type, related, _norm_finding_text(flag.message))


def _evidence_score(finding: ExtractionFinding) -> tuple[int, int, int]:
    evidence = finding.evidence
    if evidence is None:
        return (0, 0, 0)
    quote = (evidence.quote or "").strip()
    has_quote = 1 if quote else 0
    has_image = 1 if (evidence.image_id or evidence.image_index is not None) else 0
    return (has_quote + has_image, has_image, len(quote))


def _preferred_finding_id(finding: ExtractionFinding) -> int:
    related = finding.related_ids
    if len(related) == 1 and finding.id == f"find_{related[0]}":
        return 2
    if finding.id.startswith(("find_missing_qty_", "find_contradiction_")):
        return 1
    return 0


def _merge_evidence(
    left: SourceEvidence | None, right: SourceEvidence | None
) -> SourceEvidence | None:
    if right is None:
        return left.model_copy() if left is not None else None
    if left is None:
        return right.model_copy()
    quote_left = (left.quote or "").strip()
    quote_right = (right.quote or "").strip()
    quote = quote_left if len(quote_left) >= len(quote_right) else quote_right
    return SourceEvidence(
        quote=quote or None,
        image_id=left.image_id or right.image_id,
        image_index=(
            left.image_index if left.image_index is not None else right.image_index
        ),
    )


def _merge_findings(
    keeper: ExtractionFinding, other: ExtractionFinding
) -> ExtractionFinding:
    aliases = []
    for value in [keeper.id, *keeper.alias_ids, other.id, *other.alias_ids]:
        if value not in aliases:
            aliases.append(value)
    message = keeper.message
    if len(_norm_finding_text(other.message)) > len(_norm_finding_text(message)):
        message = other.message
    merged_id = keeper.id
    if _preferred_finding_id(other) > _preferred_finding_id(keeper):
        merged_id = other.id
    aliases = [value for value in aliases if value != merged_id]
    return ExtractionFinding(
        id=merged_id,
        type=keeper.type,
        message=message,
        related_ids=list(dict.fromkeys([*keeper.related_ids, *other.related_ids])),
        evidence=_merge_evidence(keeper.evidence, other.evidence),
        alias_ids=aliases,
    )


def canonicalize_findings(result: ExtractionResult) -> dict[str, str]:
    """Collapse duplicate findings into one canonical review item. In-place."""
    grouped: dict[tuple, ExtractionFinding] = {}
    order: list[tuple] = []
    for finding in result.findings:
        key = semantic_finding_key(finding)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = finding.model_copy(deep=True)
            order.append(key)
            continue
        keeper, other = existing, finding
        if _evidence_score(other) > _evidence_score(keeper):
            keeper, other = finding, existing
        elif _evidence_score(other) == _evidence_score(keeper) and (
            _preferred_finding_id(other) > _preferred_finding_id(keeper)
        ):
            keeper, other = finding, existing
        grouped[key] = _merge_findings(keeper.model_copy(deep=True), other)

    result.findings = [grouped[key] for key in order]
    aliases = {item.id: item.id for item in result.findings}
    for item in result.findings:
        for alias in item.alias_ids:
            aliases[alias] = item.id
    canonicalize_review_flags(result)
    return aliases


def canonicalize_review_flags(result: ExtractionResult) -> None:
    grouped: dict[tuple, ReviewFlag] = {}
    order: list[tuple] = []
    for flag in result.review_flags:
        key = _flag_key(flag)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = flag.model_copy(deep=True)
            order.append(key)
            continue
        message = existing.message
        if len(_norm_finding_text(flag.message)) > len(_norm_finding_text(message)):
            message = flag.message
        grouped[key] = ReviewFlag(
            type=existing.type,
            message=message,
            related_ids=list(dict.fromkeys([*existing.related_ids, *flag.related_ids])),
        )
    result.review_flags = [grouped[key] for key in order]


def finding_id_map(result: ExtractionResult) -> dict[str, str]:
    mapping = {item.id: item.id for item in result.findings}
    for item in result.findings:
        for alias in item.alias_ids:
            mapping[alias] = item.id
    return mapping


def remap_review_decisions(
    decisions: list[ReviewDecision], result: ExtractionResult
) -> list[ReviewDecision]:
    """Map alias finding ids onto the canonical finding so duplicates share one decision."""
    aliases = finding_id_map(result)
    rank = {"rejected": 0, "accepted": 1, "edited": 2}
    collapsed: dict[str, ReviewDecision] = {}
    for decision in decisions:
        canonical_id = aliases.get(decision.finding_id, decision.finding_id)
        remapped = decision.model_copy(update={"finding_id": canonical_id})
        previous = collapsed.get(canonical_id)
        if previous is None:
            collapsed[canonical_id] = remapped
            continue
        previous_rank = rank.get(previous.status, 0)
        next_rank = rank.get(remapped.status, 0)
        if next_rank > previous_rank:
            collapsed[canonical_id] = remapped
        elif next_rank == previous_rank and remapped.decided_at >= previous.decided_at:
            collapsed[canonical_id] = remapped
    return list(collapsed.values())


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


def _has_qualifier_note(ingredient: ExtractedIngredient) -> bool:
    notes = (ingredient.notes or "").strip()
    if is_qualifier_phrase(notes):
        return True
    lowered = notes.lower()
    return any(
        phrase in lowered
        for phrase in ("to taste", "as needed", "for garnish", "divided")
    )


def ensure_missing_quantity_findings(result: ExtractionResult) -> None:
    existing = {_finding_key(f) for f in result.findings}
    for ingredient in result.listed_ingredients():
        if ingredient.quantity:
            continue
        if _has_qualifier_note(ingredient):
            continue
        message = "No amount was given in the source"
        finding = ExtractionFinding(
            id=f"find_missing_qty_{ingredient.id}",
            type="missing_quantity",
            message=message,
            related_ids=[ingredient.id],
            evidence=ingredient.evidence,
        )
        if _finding_key(finding) in existing:
            continue
        if any(
            f.type == "missing_quantity" and ingredient.id in f.related_ids
            for f in result.findings
        ):
            continue
        result.findings.append(finding)
        existing.add(_finding_key(finding))
        if not any(
            f.type == "missing_quantity" and ingredient.id in f.related_ids
            for f in result.review_flags
        ):
            result.review_flags.append(
                ReviewFlag(
                    type="missing_quantity",
                    message=message,
                    related_ids=[ingredient.id],
                )
            )


def sanitize_instruction_only_amounts(result: ExtractionResult) -> None:
    """Keep instruction amounts only when the source states them; never invent."""
    for ingredient in result.instruction_only_ingredients():
        source_amount = source_amount_for_instruction_ingredient(result, ingredient)
        if source_amount:
            ingredient.quantity, ingredient.unit = source_amount
        else:
            ingredient.quantity = None
            ingredient.unit = None


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
    sanitize_instruction_only_amounts(result)
    apply_quantity_consistency_checks(result)
    ensure_missing_quantity_findings(result)
    sync_review_flags_into_findings(result)
    canonicalize_findings(result)
    return result
