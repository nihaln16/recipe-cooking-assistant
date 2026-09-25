"""Apply user review decisions onto a working copy of an extracted recipe."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from recipe_cooking_assistant.db import isoformat, utcnow
from recipe_cooking_assistant.ingredient_display import format_ingredient_line
from recipe_cooking_assistant.models import (
    ExtractedIngredient,
    ExtractedStep,
    ExtractionFinding,
    ExtractionResult,
    ReviewDecision,
)
from recipe_cooking_assistant.normalize import finding_id_map
from recipe_cooking_assistant.quantity_consistency import (
    extract_source_amount,
    source_amount_for_instruction_ingredient,
)

MAX_NAME_LEN = 200
MAX_AMOUNT_LEN = 40
MAX_TEXT_LEN = 2000

PRIMARY_BY_TYPE: dict[str, tuple[str, str]] = {
    "instruction_only_ingredient": ("add_to_ingredients", "Add to ingredients"),
    "contradiction": ("keep_listed_amount", "Keep listed amount"),
    "missing_quantity": ("keep_unspecified", "Keep unspecified"),
    "uncertain_extraction": ("keep_as_extracted", "Keep as extracted"),
    "uncertain_ordering": ("keep_as_extracted", "Keep as extracted"),
    "other": ("keep_as_extracted", "Keep as extracted"),
}


class ReviewError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def primary_action_for(finding_type: str) -> tuple[str, str]:
    return PRIMARY_BY_TYPE.get(
        finding_type, ("keep_as_extracted", "Keep as extracted")
    )


def _norm(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text or None


def content_badge(item: Any) -> str:
    markers: list[str] = []
    for field in ("name", "quantity", "unit", "notes", "text"):
        specific = getattr(item, f"{field}_provenance", None)
        if specific:
            markers.append(specific)
    if "user_edit" in markers or getattr(item, "provenance", None) == "user_edit":
        return "User edit"
    if "ai_suggestion" in markers:
        return "AI suggestion"
    if "calculated" in markers or getattr(item, "provenance", None) == "calculated":
        return "Calculated"
    if (
        getattr(item, "provenance", None) == "needs_review"
        or getattr(item, "confidence", None) == "uncertain"
    ):
        return "Needs review"
    return "Source"


def _set_field(
    item: Any, field: str, new_value: str | None, *, source_derived: bool
) -> None:
    new_n = _norm(new_value)
    old_n = _norm(getattr(item, field, None))
    if old_n == new_n:
        return
    setattr(item, field, new_n)
    setattr(
        item,
        f"{field}_provenance",
        "source" if source_derived else "user_edit",
    )


def related_ingredient(
    result: ExtractionResult, finding: ExtractionFinding
) -> ExtractedIngredient | None:
    by_id = {item.id: item for item in result.ingredients}
    for related_id in finding.related_ids:
        item = by_id.get(related_id)
        if item is not None:
            return item
    return None


def related_step(
    result: ExtractionResult, finding: ExtractionFinding
) -> ExtractedStep | None:
    by_id = {item.id: item for item in result.steps}
    for related_id in finding.related_ids:
        item = by_id.get(related_id)
        if item is not None:
            return item
    return None


def contradiction_amounts(
    result: ExtractionResult, finding: ExtractionFinding
) -> tuple[str | None, str | None]:
    ingredient = related_ingredient(result, finding)
    step = related_step(result, finding)
    listed = None
    if ingredient and (ingredient.quantity or ingredient.unit):
        listed = " ".join(
            part for part in (ingredient.quantity, ingredient.unit) if part
        )
    step_label = None
    if ingredient and step:
        parsed = extract_source_amount(step.text, ingredient)
        if parsed is None and step.source_direction_text:
            parsed = extract_source_amount(step.source_direction_text, ingredient)
        if parsed:
            step_label = f"{parsed[0]} {parsed[1]}"
    return listed, step_label


def recommendation_text(
    finding: ExtractionFinding, result: ExtractionResult
) -> str:
    ingredient = related_ingredient(result, finding)
    if finding.type == "instruction_only_ingredient":
        amount = None
        if ingredient:
            parsed = source_amount_for_instruction_ingredient(result, ingredient)
            if parsed:
                amount = f"{parsed[0]} {parsed[1]}"
        if amount:
            return (
                f"Add this mention to the ingredient list using the source amount "
                f"({amount}). This does not invent a quantity."
            )
        return (
            "Add this mention to the ingredient list. The source does not give "
            "an amount, so quantity stays unspecified."
        )
    if finding.type == "contradiction":
        listed, step_amount = contradiction_amounts(result, finding)
        if listed and step_amount:
            return (
                f"Keep the listed amount ({listed}). The step amount "
                f"({step_amount}) stays as conflicting source evidence."
            )
        return "Keep the listed amount. The conflicting step line stays as evidence."
    if finding.type == "missing_quantity":
        return (
            "Keep this amount unspecified. Enter a quantity only if you want to "
            "add one yourself."
        )
    return "Keep the extracted text as-is. Confirming does not change source facts."


@dataclass
class ReviewCard:
    finding: ExtractionFinding
    decision: ReviewDecision | None
    recommendation: str
    primary_action: str
    primary_label: str
    related_line: str
    listed_amount: str | None = None
    step_amount: str | None = None
    ingredient: ExtractedIngredient | None = None
    step: ExtractedStep | None = None
    resolution_summary: str | None = None


def resolution_summary(finding: ExtractionFinding, decision: ReviewDecision | None) -> str | None:
    if decision is None:
        return None
    resolution = decision.resolution or {}
    amount = " ".join(
        part
        for part in (resolution.get("quantity"), resolution.get("unit"))
        if part
    )
    if decision.status == "rejected":
        return "Left unchanged"
    if finding.type == "missing_quantity":
        if decision.status == "edited" and amount:
            return f"Set to {amount}"
        if decision.status == "edited":
            return "Set a custom amount"
        return "Kept unspecified"
    if finding.type == "instruction_only_ingredient":
        if decision.status == "edited" and amount:
            return f"Added as {amount}"
        if decision.status == "edited":
            return "Added with edits"
        return "Added to ingredients"
    if finding.type == "contradiction":
        kind = resolution.get("kind")
        if kind in {"keep_listed_amount", "listed"}:
            return "Kept listed amount"
        if kind in {"use_step_amount", "step"}:
            return "Used step amount"
        if amount:
            return f"Set to {amount}"
        return "Set a custom amount"
    if decision.status == "edited":
        return "Saved edits"
    return "Kept as extracted"


def build_review_cards(
    original: ExtractionResult,
    working: ExtractionResult,
    decisions: list[ReviewDecision],
) -> list[ReviewCard]:
    by_finding = {item.finding_id: item for item in decisions}
    cards: list[ReviewCard] = []
    for finding in original.findings:
        action, label = primary_action_for(finding.type)
        ingredient = related_ingredient(working, finding) or related_ingredient(
            original, finding
        )
        step = related_step(working, finding) or related_step(original, finding)
        listed_amount, step_amount = contradiction_amounts(original, finding)
        if ingredient is not None:
            related_line = format_ingredient_line(ingredient)
        elif step is not None:
            related_line = step.text
        else:
            related_line = finding.message
        cards.append(
            ReviewCard(
                finding=finding,
                decision=by_finding.get(finding.id),
                recommendation=recommendation_text(finding, original),
                primary_action=action,
                primary_label=label,
                related_line=related_line,
                listed_amount=listed_amount,
                step_amount=step_amount,
                ingredient=ingredient,
                step=step,
                resolution_summary=resolution_summary(finding, by_finding.get(finding.id)),
            )
        )
    return cards


def apply_decisions(
    result: ExtractionResult, decisions: list[ReviewDecision]
) -> ExtractionResult:
    working = result.model_copy(deep=True)
    aliases = finding_id_map(working)
    findings = {item.id: item for item in working.findings}
    for decision in decisions:
        if decision.status == "rejected":
            continue
        canonical_id = aliases.get(decision.finding_id, decision.finding_id)
        finding = findings.get(canonical_id)
        if finding is None:
            continue
        _apply_one(working, finding, decision)
    return working


def _apply_one(
    working: ExtractionResult,
    finding: ExtractionFinding,
    decision: ReviewDecision,
) -> None:
    resolution = decision.resolution or {}
    kind = resolution.get("kind")
    if not kind:
        if decision.status == "accepted":
            kind = primary_action_for(finding.type)[0]
        else:
            kind = "edit"

    if finding.type == "instruction_only_ingredient":
        _apply_instruction_only(working, finding, kind, resolution)
    elif finding.type == "contradiction":
        _apply_contradiction(working, finding, kind, resolution)
    elif finding.type == "missing_quantity":
        _apply_missing_quantity(working, finding, kind, resolution)
    else:
        _apply_uncertain(working, finding, kind, resolution)


def _apply_instruction_only(
    working: ExtractionResult,
    finding: ExtractionFinding,
    kind: str,
    resolution: dict[str, Any],
) -> None:
    ingredient = related_ingredient(working, finding)
    if ingredient is None:
        return
    ingredient.list_status = "listed"
    source_amount = source_amount_for_instruction_ingredient(working, ingredient)
    if kind == "add_to_ingredients":
        if source_amount and not ingredient.quantity:
            ingredient.quantity, ingredient.unit = source_amount
        return

    if "name" in resolution:
        _set_field(
            ingredient, "name", resolution.get("name"), source_derived=False
        )
    if "quantity" in resolution:
        _set_field(
            ingredient,
            "quantity",
            resolution.get("quantity"),
            source_derived=False,
        )
    if "unit" in resolution:
        _set_field(
            ingredient, "unit", resolution.get("unit"), source_derived=False
        )
    if (
        "quantity" not in resolution
        and source_amount
        and not ingredient.quantity
    ):
        ingredient.quantity, ingredient.unit = source_amount


def _apply_contradiction(
    working: ExtractionResult,
    finding: ExtractionFinding,
    kind: str,
    resolution: dict[str, Any],
) -> None:
    ingredient = related_ingredient(working, finding)
    step = related_step(working, finding)
    if ingredient is None:
        return
    if kind in {"keep_listed_amount", "listed"}:
        return
    if kind in {"use_step_amount", "step"} and step is not None:
        parsed = extract_source_amount(step.text, ingredient)
        if parsed is None and step.source_direction_text:
            parsed = extract_source_amount(step.source_direction_text, ingredient)
        if parsed:
            _set_field(
                ingredient, "quantity", parsed[0], source_derived=True
            )
            _set_field(ingredient, "unit", parsed[1], source_derived=True)
        return
    if "quantity" in resolution:
        _set_field(
            ingredient,
            "quantity",
            resolution.get("quantity"),
            source_derived=False,
        )
    if "unit" in resolution:
        _set_field(
            ingredient, "unit", resolution.get("unit"), source_derived=False
        )


def _apply_missing_quantity(
    working: ExtractionResult,
    finding: ExtractionFinding,
    kind: str,
    resolution: dict[str, Any],
) -> None:
    ingredient = related_ingredient(working, finding)
    if ingredient is None:
        return
    if kind == "keep_unspecified":
        return
    if "quantity" in resolution:
        _set_field(
            ingredient,
            "quantity",
            resolution.get("quantity"),
            source_derived=False,
        )
    if "unit" in resolution:
        _set_field(
            ingredient, "unit", resolution.get("unit"), source_derived=False
        )


def _apply_uncertain(
    working: ExtractionResult,
    finding: ExtractionFinding,
    kind: str,
    resolution: dict[str, Any],
) -> None:
    if kind == "keep_as_extracted":
        return
    ingredient = related_ingredient(working, finding)
    step = related_step(working, finding)
    if "name" in resolution and ingredient is not None:
        _set_field(
            ingredient, "name", resolution.get("name"), source_derived=False
        )
    if "quantity" in resolution and ingredient is not None:
        _set_field(
            ingredient,
            "quantity",
            resolution.get("quantity"),
            source_derived=False,
        )
    if "unit" in resolution and ingredient is not None:
        _set_field(
            ingredient, "unit", resolution.get("unit"), source_derived=False
        )
    if "text" in resolution and step is not None:
        _set_field(step, "text", resolution.get("text"), source_derived=False)


def _bounded(value: str | None, limit: int, label: str) -> str | None:
    text = _norm(value)
    if text is None:
        return None
    if len(text) > limit:
        raise ReviewError(f"{label} is too long.")
    return text


def parse_review_action(
    *,
    result: ExtractionResult,
    finding_id: str,
    action: str,
    form: dict[str, str | None],
) -> ReviewDecision:
    action = (action or "").strip().lower()
    if action not in {"accept", "edit", "reject"}:
        raise ReviewError("Choose a valid review action.")

    finding = next((item for item in result.findings if item.id == finding_id), None)
    if finding is None:
        aliases = finding_id_map(result)
        canonical_id = aliases.get(finding_id)
        if canonical_id:
            finding = next(
                (item for item in result.findings if item.id == canonical_id), None
            )
    if finding is None:
        raise ReviewError("That review item was not found on this recipe.")

    decided_at = isoformat(utcnow())
    if action == "reject":
        return ReviewDecision(
            finding_id=finding.id,
            status="rejected",
            resolution={},
            decided_at=decided_at,
        )
    if action == "accept":
        kind, _ = primary_action_for(finding.type)
        return ReviewDecision(
            finding_id=finding.id,
            status="accepted",
            resolution={"kind": kind},
            decided_at=decided_at,
        )
    return ReviewDecision(
        finding_id=finding.id,
        status="edited",
        resolution=_parse_edit(result, finding, form),
        decided_at=decided_at,
    )


def _parse_edit(
    result: ExtractionResult,
    finding: ExtractionFinding,
    form: dict[str, str | None],
) -> dict[str, Any]:
    if finding.type == "instruction_only_ingredient":
        name = _bounded(form.get("name"), MAX_NAME_LEN, "Name")
        if not name:
            raise ReviewError("Enter an ingredient name.")
        return {
            "kind": "edit",
            "name": name,
            "quantity": _bounded(form.get("quantity"), MAX_AMOUNT_LEN, "Quantity"),
            "unit": _bounded(form.get("unit"), MAX_AMOUNT_LEN, "Unit"),
        }

    if finding.type == "contradiction":
        choice = (form.get("choice") or "").strip().lower()
        if choice in {"listed", "keep_listed_amount"}:
            return {"kind": "keep_listed_amount"}
        if choice in {"step", "use_step_amount"}:
            listed, step_amount = contradiction_amounts(result, finding)
            if not step_amount:
                raise ReviewError("The step does not include a usable amount.")
            _ = listed
            return {"kind": "use_step_amount"}
        if choice in {"custom", "edit"}:
            quantity = _bounded(form.get("quantity"), MAX_AMOUNT_LEN, "Quantity")
            unit = _bounded(form.get("unit"), MAX_AMOUNT_LEN, "Unit")
            if not quantity and not unit:
                raise ReviewError("Enter a quantity or unit.")
            return {"kind": "custom", "quantity": quantity, "unit": unit}
        raise ReviewError("Choose listed amount, step amount, or a custom amount.")

    if finding.type == "missing_quantity":
        quantity = _bounded(form.get("quantity"), MAX_AMOUNT_LEN, "Quantity")
        unit = _bounded(form.get("unit"), MAX_AMOUNT_LEN, "Unit")
        if not quantity and not unit:
            raise ReviewError("Enter a quantity or unit, or keep it unspecified.")
        return {"kind": "edit", "quantity": quantity, "unit": unit}

    resolution: dict[str, Any] = {"kind": "edit"}
    name = _bounded(form.get("name"), MAX_NAME_LEN, "Name")
    text = _bounded(form.get("text"), MAX_TEXT_LEN, "Text")
    quantity = _bounded(form.get("quantity"), MAX_AMOUNT_LEN, "Quantity")
    unit = _bounded(form.get("unit"), MAX_AMOUNT_LEN, "Unit")
    if name:
        resolution["name"] = name
    if text:
        resolution["text"] = text
    if quantity is not None or unit is not None:
        if form.get("quantity") is not None:
            resolution["quantity"] = quantity
        if form.get("unit") is not None:
            resolution["unit"] = unit
    if len(resolution) == 1:
        raise ReviewError("Edit at least one field, or keep the extracted text.")
    return resolution
