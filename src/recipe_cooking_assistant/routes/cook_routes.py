from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.cooking import (
    CookCursor,
    CookNavigationError,
    cursor_for,
    ingredients_for_step,
    parse_step_number,
    resume_index,
    store_cursor,
)
from recipe_cooking_assistant.db import Database
from recipe_cooking_assistant.deps import get_db, get_session_id, get_settings
from recipe_cooking_assistant.routes.recipe_routes import not_found_response, prepare_recipe
from recipe_cooking_assistant.templating import create_templates

templates = create_templates()

router = APIRouter(tags=["cooking"])

REVIEW_BLOCKED = (
    "Decide every review item before cooking. "
    "Nothing on this recipe has been accepted, edited, or rejected for you."
)
INVALID_STEP = "That step is not part of this recipe."
INVALID_ACTION = "Choose Back, Next, or Finish."


def _page(
    request: Request,
    settings: Settings,
    *,
    status_code: int = 200,
    **context,
) -> HTMLResponse:
    payload = {"app_name": settings.app_name, **context}
    return templates.TemplateResponse(
        request, "cook.html", payload, status_code=status_code
    )


def _blocked(request: Request, settings: Settings, recipe_id: str) -> HTMLResponse:
    return _page(
        request,
        settings,
        status_code=400,
        mode="blocked",
        recipe_id=recipe_id,
        heading="Review this recipe first",
        message=REVIEW_BLOCKED,
        recipe=None,
    )


def _invalid(
    request: Request, settings: Settings, recipe_id: str, title: str | None
) -> HTMLResponse:
    return _page(
        request,
        settings,
        status_code=400,
        mode="invalid",
        recipe_id=recipe_id,
        heading="That step is unavailable",
        message=INVALID_STEP,
        title=title,
        recipe=None,
    )


@router.get("/recipes/{recipe_id}/cook", response_class=HTMLResponse)
def cook_resume(
    request: Request,
    recipe_id: str,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)
    if prepared.pending:
        return _blocked(request, settings, prepared.stored.id)

    steps = prepared.working.steps
    cursor = cursor_for(request.session, prepared.stored.id)
    if not steps:
        return _page(
            request,
            settings,
            mode="empty",
            recipe_id=prepared.stored.id,
            recipe=prepared.working,
        )
    if cursor.done:
        return _page(
            request,
            settings,
            mode="done",
            recipe_id=prepared.stored.id,
            recipe=prepared.working,
        )

    index = resume_index(cursor, len(steps))
    if index != cursor.index:
        store_cursor(
            request.session,
            prepared.stored.id,
            CookCursor(index=index, done=False),
        )
    return RedirectResponse(
        url=f"/recipes/{prepared.stored.id}/cook/{index + 1}",
        status_code=303,
    )


@router.get("/recipes/{recipe_id}/cook/{step_token}", response_class=HTMLResponse)
def cook_step(
    request: Request,
    recipe_id: str,
    step_token: str,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)
    if prepared.pending:
        return _blocked(request, settings, prepared.stored.id)

    try:
        number = parse_step_number(step_token)
    except CookNavigationError:
        return _invalid(
            request, settings, prepared.stored.id, prepared.working.title
        )

    steps = prepared.working.steps
    if number > len(steps):
        return _invalid(
            request, settings, prepared.stored.id, prepared.working.title
        )

    store_cursor(
        request.session,
        prepared.stored.id,
        CookCursor(index=number - 1, done=False),
    )
    step = steps[number - 1]
    return _page(
        request,
        settings,
        mode="step",
        recipe_id=prepared.stored.id,
        recipe=prepared.working,
        step=step,
        step_number=number,
        step_count=len(steps),
        step_groups=ingredients_for_step(prepared.working, step.related_ingredient_ids),
    )


@router.post("/recipes/{recipe_id}/cook", response_class=HTMLResponse)
def cook_action(
    request: Request,
    recipe_id: str,
    action: str = Form(default=""),
    step: str = Form(default=""),
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)
    if prepared.pending:
        return _blocked(request, settings, prepared.stored.id)

    recipe_key = prepared.stored.id
    steps = prepared.working.steps
    action = (action or "").strip().lower()

    if action == "cook_again":
        store_cursor(request.session, recipe_key, CookCursor(index=0, done=False))
        if not steps:
            return RedirectResponse(url=f"/recipes/{recipe_key}/cook", status_code=303)
        return RedirectResponse(url=f"/recipes/{recipe_key}/cook/1", status_code=303)

    if action not in {"back", "next", "finish"}:
        return _page(
            request,
            settings,
            status_code=400,
            mode="invalid",
            recipe_id=recipe_key,
            heading="Choose a cooking control",
            message=INVALID_ACTION,
            title=prepared.working.title,
            recipe=prepared.working,
        )

    try:
        number = parse_step_number(step)
    except CookNavigationError:
        return _invalid(request, settings, recipe_key, prepared.working.title)
    if not steps or number > len(steps):
        return _invalid(request, settings, recipe_key, prepared.working.title)

    if action == "back":
        target = max(1, number - 1)
        store_cursor(
            request.session, recipe_key, CookCursor(index=target - 1, done=False)
        )
        return RedirectResponse(
            url=f"/recipes/{recipe_key}/cook/{target}", status_code=303
        )

    if action == "finish" or number == len(steps):
        store_cursor(
            request.session,
            recipe_key,
            CookCursor(index=number - 1, done=True),
        )
        return RedirectResponse(url=f"/recipes/{recipe_key}/cook", status_code=303)

    target = number + 1
    store_cursor(
        request.session, recipe_key, CookCursor(index=target - 1, done=False)
    )
    return RedirectResponse(
        url=f"/recipes/{recipe_key}/cook/{target}", status_code=303
    )
