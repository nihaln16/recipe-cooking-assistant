"""Direct user edits, stored apart from extraction and review decisions.

Applied after review decisions when building the working recipe. Never writes
extracted_recipes.payload_json and never creates findings or review decisions.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from recipe_cooking_assistant.models import (
    ExtractedIngredient,
    ExtractedStep,
    ExtractionResult,
)
from recipe_cooking_assistant.review import MAX_AMOUNT_LEN, MAX_NAME_LEN, MAX_TEXT_LEN, _set_field

MAX_SERVINGS_LEN = 40
MAX_NOTES_LEN = 200


class EditError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class RecipeEdits(BaseModel):
    title: str | None = None
    servings: str | None = None
    servings_edited: bool = False
    ingredient_fields: dict[str, dict[str, str | None]] = Field(default_factory=dict)
    removed_ingredient_ids: list[str] = Field(default_factory=list)
    added_ingredients: list[dict[str, Any]] = Field(default_factory=list)
    step_fields: dict[str, dict[str, str | None]] = Field(default_factory=dict)
    removed_step_ids: list[str] = Field(default_factory=list)
    added_steps: list[dict[str, Any]] = Field(default_factory=list)
    step_order: list[str] | None = None
    next_ingredient_seq: int = 1
    next_step_seq: int = 1

    def is_empty(self) -> bool:
        return not any(
            (
                self.title is not None,
                self.servings_edited,
                self.ingredient_fields,
                self.removed_ingredient_ids,
                self.added_ingredients,
                self.step_fields,
                self.removed_step_ids,
                self.added_steps,
                self.step_order,
            )
        )


def _norm(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text or None


def _bounded(value: str | None, limit: int, label: str, *, required: bool = False) -> str | None:
    text = _norm(value)
    if text is None:
        if required:
            raise EditError(f"Enter {label.lower()}.")
        return None
    if len(text) > limit:
        raise EditError(f"{label} is too long.")
    return text


def apply_direct_edits(reviewed: ExtractionResult, edits: RecipeEdits) -> ExtractionResult:
    """Overlay direct edits on the reviewed working recipe. Deterministic."""
    working = reviewed.model_copy(deep=True)
    if edits.title is not None:
        working.title = edits.title
        working.title_provenance = "user_edit"
    if edits.servings_edited:
        working.servings = edits.servings
        working.servings_provenance = "user_edit"

    by_ingredient = {item.id: item for item in working.ingredients}
    for ingredient_id, fields in edits.ingredient_fields.items():
        item = by_ingredient.get(ingredient_id)
        if item is None or ingredient_id in edits.removed_ingredient_ids:
            continue
        for field in ("name", "quantity", "unit", "notes"):
            if field in fields:
                _set_field(item, field, fields.get(field), source_derived=False)

    removed_ingredients = set(edits.removed_ingredient_ids)
    working.ingredients = [
        item for item in working.ingredients if item.id not in removed_ingredients
    ]
    for raw in edits.added_ingredients:
        working.ingredients.append(_added_ingredient(raw))

    by_step = {item.id: item for item in working.steps}
    removed_steps = set(edits.removed_step_ids)
    for step_id, fields in edits.step_fields.items():
        item = by_step.get(step_id)
        if item is None or step_id in removed_steps:
            continue
        if "text" in fields:
            _set_field(item, "text", fields.get("text"), source_derived=False)

    kept = [item for item in working.steps if item.id not in removed_steps]
    for raw in edits.added_steps:
        kept.append(_added_step(raw))
    working.steps = _order_steps(kept, edits.step_order)
    return working


def _added_ingredient(raw: dict[str, Any]) -> ExtractedIngredient:
    return ExtractedIngredient(
        id=str(raw["id"]),
        name=str(raw.get("name") or ""),
        quantity=raw.get("quantity"),
        unit=raw.get("unit"),
        notes=raw.get("notes"),
        list_status="listed",
        optional=bool(raw.get("optional")),
        provenance="user_edit",
        name_provenance="user_edit",
        quantity_provenance="user_edit" if raw.get("quantity") else None,
        unit_provenance="user_edit" if raw.get("unit") else None,
        notes_provenance="user_edit" if raw.get("notes") else None,
    )


def _added_step(raw: dict[str, Any]) -> ExtractedStep:
    return ExtractedStep(
        id=str(raw["id"]),
        text=str(raw.get("text") or ""),
        provenance="user_edit",
        text_provenance="user_edit",
    )


def _order_steps(steps: list[ExtractedStep], order: list[str] | None) -> list[ExtractedStep]:
    if not order:
        return steps
    by_id = {item.id: item for item in steps}
    ordered: list[ExtractedStep] = []
    seen: set[str] = set()
    for step_id in order:
        item = by_id.get(step_id)
        if item is None or step_id in seen:
            continue
        ordered.append(item)
        seen.add(step_id)
    for item in steps:
        if item.id not in seen:
            ordered.append(item)
    return ordered


def reviewed_ingredient_ids(reviewed: ExtractionResult) -> set[str]:
    return {item.id for item in reviewed.ingredients}


def reviewed_step_ids(reviewed: ExtractionResult) -> set[str]:
    return {item.id for item in reviewed.steps}


def _working_step_ids(reviewed: ExtractionResult, edits: RecipeEdits) -> list[str]:
    if edits.step_order:
        return list(edits.step_order)
    ids = [item.id for item in reviewed.steps if item.id not in set(edits.removed_step_ids)]
    ids.extend(str(item["id"]) for item in edits.added_steps)
    return ids


def apply_edit_action(
    reviewed: ExtractionResult,
    edits: RecipeEdits,
    action: str,
    form: dict[str, str | None],
) -> RecipeEdits:
    """Return a new edit document. Raises EditError and does not mutate findings."""
    updated = edits.model_copy(deep=True)
    action = (action or "").strip().lower()
    if action == "save_overview":
        _save_overview(reviewed, updated, form)
    elif action == "revert_overview":
        updated.title = None
        updated.servings = None
        updated.servings_edited = False
    elif action == "save_ingredient":
        _save_ingredient(reviewed, updated, form)
    elif action == "add_ingredient":
        _add_ingredient(updated, form)
    elif action == "remove_ingredient":
        _remove_ingredient(reviewed, updated, form)
    elif action == "restore_ingredient":
        _restore_ingredient(updated, form)
    elif action == "revert_ingredient":
        _revert_ingredient(reviewed, updated, form)
    elif action == "save_step":
        _save_step(reviewed, updated, form)
    elif action == "add_step":
        _add_step(updated, form)
    elif action == "remove_step":
        _remove_step(reviewed, updated, form)
    elif action == "restore_step":
        _restore_step(updated, form)
    elif action == "revert_step":
        _revert_step(reviewed, updated, form)
    elif action == "move_step":
        _move_step(reviewed, updated, form)
    elif action == "revert_order":
        updated.step_order = None
    else:
        raise EditError("Choose a valid edit action.")
    return updated


def _save_overview(
    reviewed: ExtractionResult, edits: RecipeEdits, form: dict[str, str | None]
) -> None:
    title = _bounded(form.get("title"), MAX_NAME_LEN, "Title", required=True)
    servings = _bounded(form.get("servings"), MAX_SERVINGS_LEN, "Servings")
    if title == _norm(reviewed.title):
        edits.title = None
    else:
        edits.title = title
    reviewed_servings = _norm(reviewed.servings)
    if servings == reviewed_servings:
        edits.servings = None
        edits.servings_edited = False
    else:
        edits.servings = servings
        edits.servings_edited = True


def _listed_or_known(reviewed: ExtractionResult, ingredient_id: str) -> ExtractedIngredient | None:
    return next((item for item in reviewed.ingredients if item.id == ingredient_id), None)


def _save_ingredient(
    reviewed: ExtractionResult, edits: RecipeEdits, form: dict[str, str | None]
) -> None:
    ingredient_id = (form.get("ingredient_id") or "").strip()
    added = next(
        (item for item in edits.added_ingredients if item["id"] == ingredient_id),
        None,
    )
    source = _listed_or_known(reviewed, ingredient_id)
    if added is None and source is None:
        raise EditError("That ingredient is not on this recipe.")
    if ingredient_id in edits.removed_ingredient_ids:
        raise EditError("Restore that ingredient before editing it.")
    name = _bounded(form.get("name"), MAX_NAME_LEN, "Name", required=True)
    quantity = _bounded(form.get("quantity"), MAX_AMOUNT_LEN, "Quantity")
    unit = _bounded(form.get("unit"), MAX_AMOUNT_LEN, "Unit")
    notes = _bounded(form.get("notes"), MAX_NOTES_LEN, "Preparation note")
    optional = (form.get("optional") or "").strip() == "1"
    if added is not None:
        added["name"] = name
        added["quantity"] = quantity
        added["unit"] = unit
        added["notes"] = notes
        added["optional"] = optional
        return
    assert source is not None
    fields: dict[str, str | None] = {}
    if name != _norm(source.name):
        fields["name"] = name
    if quantity != _norm(source.quantity):
        fields["quantity"] = quantity
    if unit != _norm(source.unit):
        fields["unit"] = unit
    if notes != _norm(source.notes):
        fields["notes"] = notes
    if fields:
        edits.ingredient_fields[ingredient_id] = fields
    else:
        edits.ingredient_fields.pop(ingredient_id, None)


def _add_ingredient(edits: RecipeEdits, form: dict[str, str | None]) -> None:
    name = _bounded(form.get("name"), MAX_NAME_LEN, "Name", required=True)
    ingredient_id = f"user_ing_{edits.next_ingredient_seq}"
    edits.next_ingredient_seq += 1
    edits.added_ingredients.append(
        {
            "id": ingredient_id,
            "name": name,
            "quantity": _bounded(form.get("quantity"), MAX_AMOUNT_LEN, "Quantity"),
            "unit": _bounded(form.get("unit"), MAX_AMOUNT_LEN, "Unit"),
            "notes": _bounded(form.get("notes"), MAX_NOTES_LEN, "Preparation note"),
            "optional": (form.get("optional") or "").strip() == "1",
        }
    )


def _remove_ingredient(
    reviewed: ExtractionResult, edits: RecipeEdits, form: dict[str, str | None]
) -> None:
    ingredient_id = (form.get("ingredient_id") or "").strip()
    if any(item["id"] == ingredient_id for item in edits.added_ingredients):
        edits.added_ingredients = [
            item for item in edits.added_ingredients if item["id"] != ingredient_id
        ]
        edits.ingredient_fields.pop(ingredient_id, None)
        return
    if _listed_or_known(reviewed, ingredient_id) is None:
        raise EditError("That ingredient is not on this recipe.")
    if ingredient_id not in edits.removed_ingredient_ids:
        edits.removed_ingredient_ids.append(ingredient_id)
    edits.ingredient_fields.pop(ingredient_id, None)


def _restore_ingredient(edits: RecipeEdits, form: dict[str, str | None]) -> None:
    ingredient_id = (form.get("ingredient_id") or "").strip()
    if ingredient_id not in edits.removed_ingredient_ids:
        raise EditError("That ingredient is not removed.")
    edits.removed_ingredient_ids = [
        item for item in edits.removed_ingredient_ids if item != ingredient_id
    ]


def _revert_ingredient(
    reviewed: ExtractionResult, edits: RecipeEdits, form: dict[str, str | None]
) -> None:
    ingredient_id = (form.get("ingredient_id") or "").strip()
    if any(item["id"] == ingredient_id for item in edits.added_ingredients):
        edits.added_ingredients = [
            item for item in edits.added_ingredients if item["id"] != ingredient_id
        ]
        return
    if ingredient_id not in edits.ingredient_fields and ingredient_id not in edits.removed_ingredient_ids:
        raise EditError("That ingredient has no direct edit to revert.")
    edits.ingredient_fields.pop(ingredient_id, None)
    edits.removed_ingredient_ids = [
        item for item in edits.removed_ingredient_ids if item != ingredient_id
    ]
    _ = reviewed


def _save_step(
    reviewed: ExtractionResult, edits: RecipeEdits, form: dict[str, str | None]
) -> None:
    step_id = (form.get("step_id") or "").strip()
    text = _bounded(form.get("text"), MAX_TEXT_LEN, "Step text", required=True)
    added = next((item for item in edits.added_steps if item["id"] == step_id), None)
    if added is not None:
        added["text"] = text
        return
    source = next((item for item in reviewed.steps if item.id == step_id), None)
    if source is None:
        raise EditError("That step is not on this recipe.")
    if step_id in edits.removed_step_ids:
        raise EditError("Restore that step before editing it.")
    if text == _norm(source.text):
        edits.step_fields.pop(step_id, None)
    else:
        edits.step_fields[step_id] = {"text": text}


def _add_step(edits: RecipeEdits, form: dict[str, str | None]) -> None:
    text = _bounded(form.get("text"), MAX_TEXT_LEN, "Step text", required=True)
    step_id = f"user_step_{edits.next_step_seq}"
    edits.next_step_seq += 1
    edits.added_steps.append({"id": step_id, "text": text})
    if edits.step_order is not None:
        edits.step_order.append(step_id)


def _remove_step(
    reviewed: ExtractionResult, edits: RecipeEdits, form: dict[str, str | None]
) -> None:
    step_id = (form.get("step_id") or "").strip()
    if any(item["id"] == step_id for item in edits.added_steps):
        edits.added_steps = [item for item in edits.added_steps if item["id"] != step_id]
        if edits.step_order is not None:
            edits.step_order = [item for item in edits.step_order if item != step_id]
        return
    if not any(item.id == step_id for item in reviewed.steps):
        raise EditError("That step is not on this recipe.")
    if step_id not in edits.removed_step_ids:
        edits.removed_step_ids.append(step_id)
    edits.step_fields.pop(step_id, None)
    if edits.step_order is not None:
        edits.step_order = [item for item in edits.step_order if item != step_id]


def _restore_step(edits: RecipeEdits, form: dict[str, str | None]) -> None:
    step_id = (form.get("step_id") or "").strip()
    if step_id not in edits.removed_step_ids:
        raise EditError("That step is not removed.")
    edits.removed_step_ids = [item for item in edits.removed_step_ids if item != step_id]
    if edits.step_order is not None and step_id not in edits.step_order:
        edits.step_order.append(step_id)


def _revert_step(
    reviewed: ExtractionResult, edits: RecipeEdits, form: dict[str, str | None]
) -> None:
    step_id = (form.get("step_id") or "").strip()
    if any(item["id"] == step_id for item in edits.added_steps):
        edits.added_steps = [item for item in edits.added_steps if item["id"] != step_id]
        if edits.step_order is not None:
            edits.step_order = [item for item in edits.step_order if item != step_id]
        return
    if step_id not in edits.step_fields and step_id not in edits.removed_step_ids:
        raise EditError("That step has no direct edit to revert.")
    edits.step_fields.pop(step_id, None)
    edits.removed_step_ids = [item for item in edits.removed_step_ids if item != step_id]
    _ = reviewed


def _move_step(
    reviewed: ExtractionResult, edits: RecipeEdits, form: dict[str, str | None]
) -> None:
    step_id = (form.get("step_id") or "").strip()
    direction = (form.get("direction") or "").strip().lower()
    if direction not in {"up", "down"}:
        raise EditError("Choose move up or move down.")
    order = _working_step_ids(reviewed, edits)
    if step_id not in order:
        raise EditError("That step is not on this recipe.")
    index = order.index(step_id)
    swap = index - 1 if direction == "up" else index + 1
    if swap < 0 or swap >= len(order):
        raise EditError("That step cannot move further.")
    order[index], order[swap] = order[swap], order[index]
    natural = [
        item.id for item in reviewed.steps if item.id not in set(edits.removed_step_ids)
    ]
    natural.extend(str(item["id"]) for item in edits.added_steps)
    edits.step_order = None if order == natural else order
