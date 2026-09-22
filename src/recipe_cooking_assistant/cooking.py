"""Cooking-step rules for the reviewed working recipe.

Progress lives in the browser session. This module does not read or write
extraction payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping

from recipe_cooking_assistant.models import ExtractedIngredient, ExtractionResult

COOK_SESSION_KEY = "cook_progress"


class CookNavigationError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class CookCursor:
    index: int = 0
    done: bool = False


def cursor_for(session: Mapping[str, Any], recipe_id: str) -> CookCursor:
    raw = session.get(COOK_SESSION_KEY)
    if not isinstance(raw, dict):
        return CookCursor()
    entry = raw.get(recipe_id)
    if not isinstance(entry, dict):
        return CookCursor()
    index = entry.get("index", 0)
    done = entry.get("done", False)
    if type(index) is not int or index < 0:
        index = 0
    if type(done) is not bool:
        done = False
    return CookCursor(index=index, done=done)


def store_cursor(
    session: MutableMapping[str, Any], recipe_id: str, cursor: CookCursor
) -> None:
    current = session.get(COOK_SESSION_KEY)
    updated = dict(current) if isinstance(current, dict) else {}
    updated[recipe_id] = {"index": cursor.index, "done": cursor.done}
    session[COOK_SESSION_KEY] = updated


def parse_step_number(token: str) -> int:
    """1-based step number. Rejects zero, negatives, and non-integers."""
    if not token.isdigit():
        raise CookNavigationError("That step is not part of this recipe.")
    number = int(token)
    if number < 1:
        raise CookNavigationError("That step is not part of this recipe.")
    return number


def resume_index(cursor: CookCursor, step_count: int) -> int:
    """0-based step to reopen. An unusable saved index restarts at the first step."""
    if step_count <= 0:
        return 0
    if cursor.index >= step_count:
        return 0
    return cursor.index


def ingredients_for_step(
    working: ExtractionResult, related_ingredient_ids: list[str]
) -> list[tuple[str | None, list[ExtractedIngredient]]]:
    """Listed working ingredients linked by id, plus their alternative group.

    Unknown ids and instruction-only rows are omitted. Step text is not scanned.
    """
    listed = [item for item in working.ingredients if item.list_status == "listed"]
    by_id = {item.id: item for item in listed}
    groups: list[tuple[str | None, list[ExtractedIngredient]]] = []
    emitted: set[str] = set()
    for ingredient_id in related_ingredient_ids:
        item = by_id.get(ingredient_id)
        if item is None or item.id in emitted:
            continue
        group_id = item.alternative_group_id
        if group_id:
            members = [
                candidate
                for candidate in listed
                if candidate.alternative_group_id == group_id
            ]
            groups.append((group_id, members))
            emitted.update(member.id for member in members)
        else:
            groups.append((None, [item]))
            emitted.add(item.id)
    return groups
