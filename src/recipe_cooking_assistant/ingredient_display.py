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


# Culinary proper nouns kept capitalized; not a full title-case pass.
_PROPER_NOUNS = frozenset(
    {
        "parmesan",
        "romano",
        "pecorino",
        "cheddar",
        "gruyere",
        "gruyère",
        "gouda",
        "mozzarella",
        "provolone",
        "asiago",
        "manchego",
        "dijon",
        "worcestershire",
        "tabasco",
        "sriracha",
        "mexican",
        "italian",
        "french",
        "thai",
        "chinese",
        "japanese",
        "korean",
        "cajun",
        "creole",
        "spanish",
        "indian",
    }
)


def display_ingredient_name(name: str, *, leading_capital: bool = True) -> str:
    """Sentence-style name: leading capital only, or lowercase after a quantity."""
    return _format_name(name, leading_capital=leading_capital)


def _keep_token_capitalization(token: str, *, is_first: bool) -> bool:
    core = token.strip(".,;:()")
    if not core:
        return False
    if any(char.isupper() for char in core[1:]):
        return True
    letters = [char for char in core if char.isalpha()]
    if core.isupper() and len(letters) > 1:
        return True
    if core.lower() in _PROPER_NOUNS:
        return True
    if not is_first and core[0].isupper() and core[1:].islower():
        return True
    return False


def _format_name(name: str, *, leading_capital: bool) -> str:
    cleaned = " ".join(name.split()).strip()
    if not cleaned:
        return cleaned
    rendered: list[str] = []
    for index, token in enumerate(cleaned.split()):
        if _keep_token_capitalization(token, is_first=index == 0):
            if token.lower() in _PROPER_NOUNS:
                rendered.append(token[0].upper() + token[1:] if token else token)
            else:
                rendered.append(token)
        else:
            rendered.append(token.lower())
    text = " ".join(rendered)
    if leading_capital:
        for index, char in enumerate(text):
            if char.isalpha():
                return text[:index] + char.upper() + text[index + 1 :]
    return text


def _name_without_redundant_unit(name: str, unit: str) -> str:
    if not name or not unit:
        return name
    tokens = name.split()
    unit_stem = unit.lower().rstrip("s")
    if tokens and tokens[-1].lower().rstrip("s") == unit_stem:
        trimmed = " ".join(tokens[:-1]).strip()
        return trimmed or name
    return name


def _strip_restated_amount(notes: str, quantity: str, unit: str) -> str:
    if not notes or not quantity:
        return notes
    escaped_qty = re.escape(quantity)
    if unit:
        stem = re.escape(unit.rstrip("s"))
        unit_pat = rf"{stem}s?"
        prefix = re.compile(
            rf"(?i)^{escaped_qty}\s+{unit_pat}\b[:,]?\s*"
        )
    else:
        prefix = re.compile(rf"(?i)^{escaped_qty}\b[:,]?\s*")
    return prefix.sub("", notes).strip()


def _prep_note(ingredient: ExtractedIngredient, quantity: str, unit: str) -> str | None:
    notes = (ingredient.notes or "").strip()
    if not notes:
        return None
    if is_qualifier_phrase(notes):
        return None
    notes = _strip_restated_amount(notes, quantity, unit)
    if not notes or is_qualifier_phrase(notes):
        return None
    return notes


def _with_optional(line: str, ingredient: ExtractedIngredient) -> str:
    if ingredient.optional and "optional" not in line.lower():
        return f"{line} (optional)"
    return line


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
    """Render a conventional recipe line without mutating stored source fields."""
    quantity = (ingredient.quantity or "").strip()
    unit = (ingredient.unit or "").strip()
    raw_name = (ingredient.name or "").strip()
    notes = (ingredient.notes or "").strip()

    if is_qualifier_phrase(quantity) and not unit:
        quantity, notes = "", _merge_note(notes, quantity) if notes else quantity
        # Display-only: do not write back to the ingredient.

    qualifier = notes if is_qualifier_phrase(notes) else None
    if qualifier and not quantity and not unit:
        name = _format_name(raw_name, leading_capital=True)
        return _with_optional(f"{name} — {qualifier.lower()}", ingredient)

    package_bits = [
        part
        for part in (
            (ingredient.package_count or "").strip(),
            (ingredient.package_size or "").strip(),
            (ingredient.package_type or "").strip(),
        )
        if part
    ]
    prefix_parts = [part for part in (quantity, unit) if part]
    has_prefix = bool(prefix_parts or package_bits)
    display_name = _name_without_redundant_unit(raw_name, unit)
    name = _format_name(display_name, leading_capital=not has_prefix)
    prep = _prep_note(ingredient, quantity, unit)

    if package_bits:
        line = f"{' '.join(package_bits)} {name}".strip()
    elif prefix_parts:
        line = f"{' '.join(prefix_parts)} {name}".strip()
    else:
        line = name

    if prep:
        line = f"{line}, {prep}"

    return _with_optional(line, ingredient)


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
