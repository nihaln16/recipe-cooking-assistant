"""Ingredient qualifier handling and display formatting."""

from __future__ import annotations

import re
from recipe_cooking_assistant.models import ExtractedIngredient

QUALIFIER_PHRASES = frozenset(
    {
        "to taste",
        "as needed",
        "for garnish",
        "divided",
    }
)

_QUALIFIER_RE = re.compile(
    r"^(?P<phrase>"
    + "|".join(re.escape(p) for p in sorted(QUALIFIER_PHRASES, key=len, reverse=True))
    + r")\s*$",
    re.IGNORECASE,
)


def is_qualifier_phrase(text: str | None) -> bool:
    if not text:
        return False
    return _QUALIFIER_RE.match(text.strip()) is not None


def _merge_note(existing: str | None, phrase: str) -> str:
    phrase = phrase.strip()
    if not existing or not existing.strip():
        return phrase
    if phrase.lower() in existing.lower():
        return existing.strip()
    return f"{existing.strip()}; {phrase}"


def display_ingredient_name(name: str) -> str:
    cleaned = " ".join(name.split()).strip()
    if not cleaned:
        return cleaned
    return cleaned[0].upper() + cleaned[1:]


def normalize_ingredient_qualifiers(ingredient: ExtractedIngredient) -> None:
    """Move qualifier phrases out of quantity/unit into notes."""
    for field in ("quantity", "unit"):
        value = getattr(ingredient, field)
        if not is_qualifier_phrase(value):
            continue
        phrase = value.strip()
        setattr(ingredient, field, None)
        ingredient.notes = _merge_note(ingredient.notes, phrase)


def format_ingredient_line(ingredient: ExtractedIngredient) -> str:
    """Render a listed ingredient line preserving source meaning."""
    name = display_ingredient_name(ingredient.name)
    quantity = (ingredient.quantity or "").strip()
    unit = (ingredient.unit or "").strip()
    notes = (ingredient.notes or "").strip()
    source_text = (ingredient.source_text or "").strip()

    # Qualifier-only items always use em-dash form (Salt — to taste).
    if not quantity and not unit and is_qualifier_phrase(notes):
        line = f"{name} — {notes}"
        if ingredient.optional and "optional" not in line.lower():
            line = f"{line} (optional)"
        return line

    if is_qualifier_phrase(quantity) and not unit:
        qualifier = quantity
        extra = notes if notes and notes.lower() != qualifier.lower() else ""
        line = f"{name} — {qualifier}"
        if extra:
            line = f"{line} ({extra})"
        if ingredient.optional and "optional" not in line.lower():
            line = f"{line} (optional)"
        return line

    if source_text:
        line = source_text
        if ingredient.optional and "optional" not in line.lower():
            line = f"{line} (optional)"
        return line

    package_bits = [
        p
        for p in (
            (ingredient.package_count or "").strip(),
            (ingredient.package_size or "").strip(),
            (ingredient.package_type or "").strip(),
        )
        if p
    ]
    if package_bits and not quantity and not unit:
        package = " ".join(package_bits)
        line = f"{package} {name}".strip()
        if notes:
            line = f"{line}, {notes}"
        if ingredient.optional:
            line = f"{line} (optional)"
        return line

    prefix_parts = [p for p in (quantity, unit) if p]
    prefix = " ".join(prefix_parts)
    if prefix:
        line = f"{prefix} {name}".strip()
    else:
        line = name

    if notes:
        line = f"{line} ({notes})"

    if ingredient.optional and "optional" not in line.lower():
        line = f"{line} (optional)"
    return line


def group_listed_ingredients(
    ingredients: list[ExtractedIngredient],
) -> list[tuple[str | None, list[ExtractedIngredient]]]:
    """Group listed ingredients; alternatives share alternative_group_id."""
    listed = [i for i in ingredients if i.list_status == "listed"]
    result: list[tuple[str | None, list[ExtractedIngredient]]] = []
    emitted: set[str] = set()
    for item in listed:
        gid = item.alternative_group_id
        if not gid:
            result.append((None, [item]))
            continue
        if gid in emitted:
            continue
        members = [i for i in listed if i.alternative_group_id == gid]
        result.append((gid, members))
        emitted.add(gid)
    return result
