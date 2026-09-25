"""Deterministic serving scaling for a separate cooking view.

Does not change the extraction payload, review decisions, or direct edits.
"""

from __future__ import annotations

import hashlib
import json
import re
from fractions import Fraction
from typing import Any

from recipe_cooking_assistant.ingredient_display import is_qualifier_phrase
from recipe_cooking_assistant.models import ExtractedIngredient, ExtractionResult

_UNICODE_FRACTIONS = {
    "½": Fraction(1, 2),
    "⅓": Fraction(1, 3),
    "⅔": Fraction(2, 3),
    "¼": Fraction(1, 4),
    "¾": Fraction(3, 4),
    "⅛": Fraction(1, 8),
    "⅜": Fraction(3, 8),
    "⅝": Fraction(5, 8),
    "⅞": Fraction(7, 8),
}
_RANGE_RE = re.compile(r"\d\s*(?:to|–|—|-)\s*\d", re.IGNORECASE)
_DISCRETE_UNITS = frozenset(
    {
        "clove",
        "cloves",
        "egg",
        "eggs",
        "can",
        "cans",
        "package",
        "packages",
        "slice",
        "slices",
        "piece",
        "pieces",
        "strip",
        "strips",
        "sprig",
        "sprigs",
        "leaf",
        "leaves",
        "head",
        "heads",
        "bunch",
        "bunches",
        "stalk",
        "stalks",
        "fillet",
        "fillets",
    }
)


class ScaleError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def parse_serving_count(value: str | None) -> Fraction | None:
    """A single positive count. Ranges such as 4 to 6 are not a current count."""
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    if not text or _RANGE_RE.search(text):
        return None
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    count = Fraction(text)
    if count <= 0:
        return None
    return count


def parse_quantity(value: str | None) -> Fraction | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    if not text:
        return None
    if text in _UNICODE_FRACTIONS:
        return _UNICODE_FRACTIONS[text]
    mixed_unicode = re.fullmatch(r"(\d+)\s*([½⅓⅔¼¾⅛⅜⅝⅞])", text)
    if mixed_unicode:
        return Fraction(mixed_unicode.group(1)) + _UNICODE_FRACTIONS[mixed_unicode.group(2)]
    mixed = re.fullmatch(r"(\d+)\s+(\d+)\s*/\s*(\d+)", text)
    if mixed:
        den = int(mixed.group(3))
        if den == 0:
            return None
        return Fraction(int(mixed.group(1))) + Fraction(int(mixed.group(2)), den)
    slash = re.fullmatch(r"(\d+)\s*/\s*(\d+)", text)
    if slash:
        den = int(slash.group(2))
        if den == 0:
            return None
        return Fraction(int(slash.group(1)), den)
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return Fraction(text)
    return None


def format_amount(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    if value.denominator > 8:
        decimal = round(float(value), 2)
        return f"{decimal:g}"
    if value.numerator > value.denominator:
        whole = value.numerator // value.denominator
        rem = value - whole
        return f"{whole} {rem.numerator}/{rem.denominator}"
    return f"{value.numerator}/{value.denominator}"


def _is_discrete(unit: str | None) -> bool:
    if not unit:
        return False
    return unit.strip().lower().rstrip(".") in _DISCRETE_UNITS


def _scale_number(amount: Fraction, factor: Fraction, unit: str | None) -> tuple[str, str | None]:
    scaled = amount * factor
    if _is_discrete(unit) and scaled.denominator != 1:
        rounded = Fraction(int(scaled + Fraction(1, 2)))
        return str(rounded.numerator), format_amount(scaled)
    return format_amount(scaled), None


def _skip_reason(item: ExtractedIngredient) -> str | None:
    notes = (item.notes or "").strip()
    quantity = (item.quantity or "").strip()
    if not quantity and (is_qualifier_phrase(notes) or "garnish" in notes.lower()):
        return "qualifier"
    if not quantity and not (item.package_count or "").strip():
        return "missing"
    if quantity and parse_quantity(quantity) is None and not (item.package_count or "").strip():
        return "ambiguous"
    return None


def scale_ingredient(item: ExtractedIngredient, factor: Fraction) -> dict[str, Any]:
    reason = _skip_reason(item)
    row: dict[str, Any] = {
        "id": item.id,
        "name": item.name,
        "quantity": item.quantity,
        "unit": item.unit,
        "notes": item.notes,
        "optional": item.optional,
        "alternative_group_id": item.alternative_group_id,
        "package_count": item.package_count,
        "package_size": item.package_size,
        "package_type": item.package_type,
        "provenance": "source",
        "unrounded": None,
        "skipped_reason": reason,
    }
    if reason:
        return row
    quantity = parse_quantity(item.quantity)
    if quantity is not None:
        shown, unrounded = _scale_number(quantity, factor, item.unit)
        row["quantity"] = shown
        row["unrounded"] = unrounded
        row["provenance"] = "calculated"
    package_count = parse_quantity(item.package_count)
    if package_count is not None:
        shown, unrounded = _scale_number(package_count, factor, item.package_type or "cans")
        row["package_count"] = shown
        if unrounded and not row["unrounded"]:
            row["unrounded"] = unrounded
        row["provenance"] = "calculated"
    return row


def working_fingerprint(working: ExtractionResult) -> str:
    payload = {
        "title": working.title,
        "servings": working.servings,
        "ingredients": [
            {
                "id": item.id,
                "name": item.name,
                "quantity": item.quantity,
                "unit": item.unit,
                "notes": item.notes,
                "optional": item.optional,
                "alternative_group_id": item.alternative_group_id,
                "package_count": item.package_count,
                "package_size": item.package_size,
                "package_type": item.package_type,
                "list_status": item.list_status,
            }
            for item in working.ingredients
        ],
        "steps": [
            {"id": step.id, "text": step.text, "related": list(step.related_ingredient_ids)}
            for step in working.steps
        ],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def build_scaled_view(working: ExtractionResult, target: str) -> dict[str, Any]:
    current = parse_serving_count(working.servings)
    target_count = parse_serving_count(target)
    if current is None:
        raise ScaleError(
            "Correct the serving label to a single count before scaling. "
            "A range such as 4 to 6 is not a current count."
        )
    if target_count is None:
        raise ScaleError("Enter a single target serving count.")
    if target_count == current:
        raise ScaleError("Choose a target count different from the current servings.")
    factor = target_count / current
    listed = [item for item in working.ingredients if item.list_status == "listed"]
    return {
        "current": format_amount(current),
        "target": format_amount(target_count),
        "factor": format_amount(factor) if factor.denominator <= 8 else f"{float(factor):g}",
        "fingerprint": working_fingerprint(working),
        "ingredients": [scale_ingredient(item, factor) for item in listed],
        "steps": [
            {
                "id": step.id,
                "number": index,
                "text": step.text,
                "related_ingredient_ids": list(step.related_ingredient_ids),
            }
            for index, step in enumerate(working.steps, start=1)
        ],
        "proposals": [],
        "confirmed": [],
    }


def guidance_note_text(view: dict[str, Any] | None) -> str:
    if not view:
        return ""
    stored = " ".join(str(view.get("guidance_note") or "").split()).strip()
    if stored:
        return stored
    parts = []
    for item in view.get("proposals") or []:
        text = " ".join(str(item.get("suggestion") or "").split()).strip()
        if text and text not in parts:
            parts.append(text)
    return " ".join(parts)


def display_recipe(working: ExtractionResult, view: dict[str, Any] | None) -> ExtractionResult:
    """In-memory cooking copy. Does not write the working recipe."""
    if not view or not view.get("quantities_confirmed"):
        return working
    shown = working.model_copy(deep=True)
    shown.servings = str(view.get("target") or shown.servings)
    by_id = {row["id"]: row for row in view.get("ingredients") or []}
    for item in shown.ingredients:
        row = by_id.get(item.id)
        if not row or row.get("provenance") != "calculated":
            continue
        item.quantity = row.get("quantity")
        item.package_count = row.get("package_count")
        item.provenance = "calculated"
        item.quantity_provenance = "calculated"
    return shown


def double_target(servings: str | None) -> str:
    current = parse_serving_count(servings)
    if current is None:
        raise ScaleError(
            "Correct the serving label to a single count before scaling. "
            "A range such as 4 to 6 is not a current count."
        )
    return format_amount(current * 2)
