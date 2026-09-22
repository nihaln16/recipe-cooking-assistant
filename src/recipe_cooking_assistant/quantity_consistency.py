"""Conservative post-extraction quantity contradiction detection."""

from __future__ import annotations

import re
from fractions import Fraction

from recipe_cooking_assistant.models import (
    ExtractedIngredient,
    ExtractionFinding,
    ExtractionResult,
    ReviewFlag,
    SourceEvidence,
)

_AMBIGUOUS_RE = re.compile(
    r"(?i)\b("
    r"some|handful|pinch|dash|splash|a little|to taste|as needed|"
    r"remaining|rest of|half (?:of )?the|most of|part of|mixture"
    r")\b"
)

_UNIT_ALIASES: dict[str, str] = {
    "cup": "cup",
    "cups": "cup",
    "tablespoon": "tablespoon",
    "tablespoons": "tablespoon",
    "tbsp": "tablespoon",
    "tbs": "tablespoon",
    "teaspoon": "teaspoon",
    "teaspoons": "teaspoon",
    "tsp": "teaspoon",
    "ounce": "ounce",
    "ounces": "ounce",
    "oz": "ounce",
    "pound": "pound",
    "pounds": "pound",
    "lb": "pound",
    "lbs": "pound",
    "gram": "gram",
    "grams": "gram",
    "g": "gram",
    "kilogram": "kilogram",
    "kilograms": "kilogram",
    "kg": "kilogram",
    "milliliter": "milliliter",
    "milliliters": "milliliter",
    "ml": "milliliter",
    "liter": "liter",
    "liters": "liter",
    "l": "liter",
    "clove": "clove",
    "cloves": "clove",
    "can": "can",
    "cans": "can",
}

_UNIT_ALT = "|".join(
    sorted((re.escape(u) for u in _UNIT_ALIASES), key=len, reverse=True)
)
_QTY = r"(?P<qty>\d+\s*/\s*\d+|\d+(?:\.\d+)?)"
_UNIT = rf"(?P<unit>{_UNIT_ALT})"
_UNIT_PLAIN = rf"(?:{_UNIT_ALT})"
_LEADING_AMOUNT_RE = re.compile(rf"(?i)^\s*{_QTY}\s*{_UNIT}\b")


def extract_source_amount(
    text: str | None, ingredient: ExtractedIngredient
) -> tuple[str, str] | None:
    """Return (qty, unit) stated in text for this ingredient, if explicit."""
    if not text:
        return None
    cleaned = " ".join(text.split())
    if not cleaned:
        return None

    name = (ingredient.name or "").strip()
    name_candidates = [name] if name else []
    tokens = name.split()
    if tokens:
        last = tokens[-1]
        if last.lower() not in {c.lower() for c in name_candidates}:
            name_candidates.append(last)

    for candidate in name_candidates:
        if not candidate:
            continue
        name_pat = re.escape(candidate)
        pattern = re.compile(
            rf"(?i){_QTY}\s*{_UNIT}(?:\s+(?!{_UNIT_PLAIN}\b)\w+){{0,3}}\s+"
            rf"(?:of\s+)?{name_pat}\b"
        )
        match = pattern.search(cleaned)
        if match:
            return match.group("qty").replace(" ", ""), match.group("unit")

    return None


def extract_leading_amount(text: str | None) -> tuple[str, str] | None:
    """Parse a leading 'N unit' from a short ingredient phrase."""
    if not text:
        return None
    match = _LEADING_AMOUNT_RE.match(" ".join(text.split()))
    if not match:
        return None
    return match.group("qty").replace(" ", ""), match.group("unit")


def source_amount_for_instruction_ingredient(
    result: ExtractionResult, ingredient: ExtractedIngredient
) -> tuple[str, str] | None:
    """Best source-backed amount for an instruction-only mention. Never invent."""
    texts: list[str] = []
    if ingredient.evidence and ingredient.evidence.quote:
        texts.append(ingredient.evidence.quote)
    if ingredient.source_text:
        texts.append(ingredient.source_text)

    for text in texts:
        found = extract_source_amount(text, ingredient)
        if found:
            return found

    for text in (ingredient.source_text, ingredient.notes):
        found = extract_leading_amount(text)
        if found:
            return found

    for step in result.steps:
        name = (ingredient.name or "").strip().lower()
        mentioned = ingredient.id in step.related_ingredient_ids
        if name and name in (step.text or "").lower():
            mentioned = True
        if name and name in (step.source_direction_text or "").lower():
            mentioned = True
        if not mentioned:
            continue
        found = extract_source_amount(step.text, ingredient)
        if found:
            return found
        if step.source_direction_text:
            found = extract_source_amount(step.source_direction_text, ingredient)
            if found:
                return found

    return None


def normalize_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    key = unit.strip().lower()
    return _UNIT_ALIASES.get(key)


def parse_quantity_number(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip().lower().replace(" ", "")
    if not text or _AMBIGUOUS_RE.search(text):
        return None
    try:
        if "/" in text:
            return float(Fraction(text))
        return float(text)
    except (ValueError, ZeroDivisionError):
        return None


def amounts_conflict(
    listed_qty: str | None,
    listed_unit: str | None,
    step_qty: str | None,
    step_unit: str | None,
) -> bool:
    """True only for clear numeric+compatible-unit conflicts."""
    left = parse_quantity_number(listed_qty)
    right = parse_quantity_number(step_qty)
    if left is None or right is None:
        return False
    left_unit = normalize_unit(listed_unit)
    right_unit = normalize_unit(step_unit)
    if not left_unit or not right_unit or left_unit != right_unit:
        return False
    return abs(left - right) > 1e-9


def extract_step_amount_for_ingredient(
    step_text: str, ingredient: ExtractedIngredient
) -> tuple[str, str] | None:
    """Return (qty, unit) from step text for this ingredient when explicit."""
    text = " ".join(step_text.split())
    if not text or _AMBIGUOUS_RE.search(text):
        # Ambiguous language in the step — do not force a contradiction.
        # Still allow if a clear "N unit name" pattern exists away from ambiguity?
        # Conservative: skip entire step when ambiguous phrases appear.
        return None

    name = (ingredient.name or "").strip()
    if not name:
        return None
    # Prefer full name; fall back to last token (e.g. "flour").
    name_candidates = [name]
    tokens = name.split()
    if tokens:
        name_candidates.append(tokens[-1])

    for candidate in name_candidates:
        name_pat = re.escape(candidate)
        pattern = re.compile(
            rf"(?i){_QTY}\s*{_UNIT}\s+(?:of\s+)?{name_pat}\b"
        )
        match = pattern.search(text)
        if match:
            return match.group("qty").replace(" ", ""), match.group("unit")
    return None


def scrub_invalid_instruction_only_findings(result: ExtractionResult) -> None:
    """Drop instruction_only findings that point at explicitly listed ingredients."""
    listed_ids = {i.id for i in result.listed_ingredients()}

    def invalid(related_ids: list[str], ftype: str) -> bool:
        return ftype == "instruction_only_ingredient" and any(
            rid in listed_ids for rid in related_ids
        )

    result.findings = [
        f
        for f in result.findings
        if not invalid(f.related_ids, f.type)
    ]
    result.review_flags = [
        f
        for f in result.review_flags
        if not invalid(f.related_ids, f.type)
    ]


def detect_quantity_contradictions(result: ExtractionResult) -> None:
    """Add contradiction findings for clear list-vs-step quantity conflicts."""
    by_id = {i.id: i for i in result.listed_ingredients()}
    existing_ing = {
        rid
        for f in result.findings
        if f.type == "contradiction"
        for rid in f.related_ids
        if rid in by_id
    }

    for step in result.steps:
        for ing_id in step.related_ingredient_ids:
            ingredient = by_id.get(ing_id)
            if ingredient is None:
                continue
            if not ingredient.quantity or not normalize_unit(ingredient.unit):
                continue
            extracted = extract_step_amount_for_ingredient(step.text, ingredient)
            if extracted is None:
                continue
            step_qty, step_unit = extracted
            if not amounts_conflict(
                ingredient.quantity, ingredient.unit, step_qty, step_unit
            ):
                continue
            if ing_id in existing_ing:
                continue

            listed_label = f"{ingredient.quantity} {ingredient.unit}".strip()
            step_label = f"{step_qty} {step_unit}".strip()
            message = (
                f"Listed amount is {listed_label} but step says {step_label}"
            )
            result.findings.append(
                ExtractionFinding(
                    id=f"find_contradiction_{ing_id}_{step.id}",
                    type="contradiction",
                    message=message,
                    related_ids=[ing_id, step.id],
                    evidence=SourceEvidence(
                        quote=step.text,
                        image_id=step.evidence.image_id if step.evidence else None,
                        image_index=(
                            step.evidence.image_index if step.evidence else None
                        ),
                    ),
                )
            )
            existing_ing.add(ing_id)
            if not any(
                flag.type == "contradiction" and ing_id in flag.related_ids
                for flag in result.review_flags
            ):
                result.review_flags.append(
                    ReviewFlag(
                        type="contradiction",
                        message=message,
                        related_ids=[ing_id, step.id],
                    )
                )


def apply_quantity_consistency_checks(result: ExtractionResult) -> None:
    scrub_invalid_instruction_only_findings(result)
    detect_quantity_contradictions(result)
