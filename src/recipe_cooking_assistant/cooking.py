"""Cooking-step rules for the reviewed working recipe.

Progress lives in the browser session. This module does not read or write
extraction payloads.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping

from recipe_cooking_assistant.models import ExtractedIngredient, ExtractionResult

COOK_SESSION_KEY = "cook_progress"
GUIDE_TOKEN_KEY = "cook_guide_token"
SUBSTITUTION_TOKEN_KEY = "substitution_guide_token"
_TOKEN_LOCK = threading.Lock()


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


def ensure_guide_token(
    session: MutableMapping[str, Any],
    recipe_id: str,
    key: str = GUIDE_TOKEN_KEY,
) -> str:
    """Return the current form token. Opening another page does not replace it."""
    current = session.get(key)
    if (
        isinstance(current, dict)
        and current.get("recipe_id") == recipe_id
        and current.get("token")
    ):
        return str(current["token"])
    token = secrets.token_urlsafe(16)
    session[key] = {"recipe_id": recipe_id, "token": token}
    return token


def issue_guide_token(session: MutableMapping[str, Any], recipe_id: str) -> str:
    """Force a new cooking-guide token. Prefer ensure_guide_token for page renders."""
    token = secrets.token_urlsafe(16)
    session[GUIDE_TOKEN_KEY] = {"recipe_id": recipe_id, "token": token}
    return token


def consume_guide_token(
    session: MutableMapping[str, Any],
    recipe_id: str,
    submitted: str,
    key: str = GUIDE_TOKEN_KEY,
) -> bool:
    """Accept a token once. A replay returns False and does not rotate again."""
    with _TOKEN_LOCK:
        current = session.get(key)
        if not isinstance(current, dict):
            return False
        if current.get("recipe_id") != recipe_id or current.get("token") != submitted:
            return False
        if not submitted:
            return False
        session[key] = {
            "recipe_id": recipe_id,
            "token": secrets.token_urlsafe(16),
        }
        return True


def apply_navigation(
    session: MutableMapping[str, Any],
    recipe_id: str,
    number: int,
    step_count: int,
    command: str,
) -> str:
    """Move the existing cursor. Returns a path beginning with /cook.

    Does not write the recipe. An invalid step raises before any cursor write.
    """
    if step_count <= 0 or number < 1 or number > step_count:
        raise CookNavigationError("That step is not part of this recipe.")
    if command == "repeat":
        store_cursor(
            session, recipe_id, CookCursor(index=number - 1, done=False)
        )
        return f"/cook/{number}"
    if command == "start_over":
        store_cursor(session, recipe_id, CookCursor(index=0, done=False))
        if step_count <= 0:
            return "/cook"
        return "/cook/1"
    if command == "back":
        target = max(1, number - 1)
        store_cursor(
            session, recipe_id, CookCursor(index=target - 1, done=False)
        )
        return f"/cook/{target}"
    if command == "finish" or (command == "next" and number == step_count):
        store_cursor(
            session, recipe_id, CookCursor(index=number - 1, done=True)
        )
        return "/cook"
    if command == "next":
        target = number + 1
        store_cursor(
            session, recipe_id, CookCursor(index=target - 1, done=False)
        )
        return f"/cook/{target}"
    raise CookNavigationError("Choose Back, Next, or Finish.")
