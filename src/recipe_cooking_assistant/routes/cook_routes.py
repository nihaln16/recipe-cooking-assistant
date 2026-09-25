from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.cooking import (
    CookCursor,
    CookNavigationError,
    apply_navigation,
    consume_guide_token,
    cursor_for,
    ingredients_for_step,
    ensure_guide_token,
    parse_step_number,
    resume_index,
    store_cursor,
)
from recipe_cooking_assistant.db import Database, parse_iso, utcnow
from recipe_cooking_assistant.deps import get_db, get_session_id, get_settings
from recipe_cooking_assistant.guidance import (
    DUPLICATE_WINDOW_SECONDS,
    EMPTY_MESSAGE,
    INVALID_QUICK_MESSAGE,
    MAX_EARLIER_MESSAGES,
    MAX_GUIDANCE_CHARS,
    MAX_STORED_PER_RECIPE,
    MAX_STORED_PER_STEP,
    MAX_USER_CHARS,
    OVERSIZE_MESSAGE,
    QUICK_ACTIONS,
    RATE_LIMIT_MESSAGE,
    SAFE_GUIDANCE_ERROR,
    UNCONFIGURED_GUIDANCE,
    GuidanceError,
    OpenAIGuidanceClient,
    build_model_context,
    navigation_command,
    quick_action_text,
)
from recipe_cooking_assistant.import_limit import ImportLimiter
from recipe_cooking_assistant.routes.recipe_routes import not_found_response, prepare_recipe
from recipe_cooking_assistant.scaling import display_recipe, working_fingerprint
from recipe_cooking_assistant.timer import AmbiguousDurations, format_clock, parse_step_timer
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
        request,
        "cook.html",
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
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


def _apply_scaled_display(db: Database, prepared) -> None:
    if prepared is None:
        return
    view = db.get_scaled_view(prepared.stored.id, prepared.stored.session_id)
    if view and view.get("fingerprint") != working_fingerprint(prepared.working):
        return
    prepared.working = display_recipe(prepared.working, view)


@router.get("/recipes/{recipe_id}/cook", response_class=HTMLResponse)
def cook_resume(
    request: Request,
    recipe_id: str,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    prepared = prepare_recipe(db, recipe_id, session_id)
    _apply_scaled_display(db, prepared)
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
    _apply_scaled_display(db, prepared)
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
    return _render_step(
        request,
        settings,
        db,
        prepared,
        number,
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
    _apply_scaled_display(db, prepared)
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

    try:
        target = apply_navigation(
            request.session,
            recipe_key,
            number,
            len(steps),
            action,
        )
    except CookNavigationError:
        return _invalid(request, settings, recipe_key, prepared.working.title)
    return RedirectResponse(url=f"/recipes/{recipe_key}{target}", status_code=303)


def _render_step(
    request: Request,
    settings: Settings,
    db: Database,
    prepared,
    number: int,
    *,
    status_code: int = 200,
    guide_error: str | None = None,
    guide_draft: str = "",
):
    step = prepared.working.steps[number - 1]
    parsed = parse_step_timer(step.text)
    messages = db.list_cook_messages(prepared.stored.id, prepared.stored.session_id, number)
    return _page(
        request,
        settings,
        status_code=status_code,
        mode="step",
        recipe_id=prepared.stored.id,
        recipe=prepared.working,
        step=step,
        step_number=number,
        step_count=len(prepared.working.steps),
        step_groups=ingredients_for_step(
            prepared.working, step.related_ingredient_ids
        ),
        messages=messages,
        quick_actions=QUICK_ACTIONS,
        form_token=ensure_guide_token(request.session, prepared.stored.id),
        guide_error=guide_error,
        guide_draft=guide_draft,
        timer_offer=parsed if not isinstance(parsed, AmbiguousDurations) else None,
        timer_clock=format_clock(parsed.seconds) if parsed and not isinstance(parsed, AmbiguousDurations) else "",
        timer_ambiguous=parsed if isinstance(parsed, AmbiguousDurations) else None,
    )


def _guidance_client(request: Request, settings: Settings):
    client = request.app.state.guidance_client
    if client is not None:
        return client
    if not settings.openai_api_key:
        return None
    return OpenAIGuidanceClient(settings.openai_api_key)


def _accepts_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _guide_client_response(
    request: Request,
    settings: Settings,
    db: Database,
    prepared,
    number: int,
    *,
    status_code: int = 200,
    guide_error: str | None = None,
    guide_draft: str = "",
    redirect: str | None = None,
    user_text: str | None = None,
    guidance: str | None = None,
):
    if not _accepts_json(request):
        if redirect:
            return RedirectResponse(url=redirect, status_code=303)
        return _render_step(
            request,
            settings,
            db,
            prepared,
            number,
            status_code=status_code,
            guide_error=guide_error,
            guide_draft=guide_draft,
        )
    token = ensure_guide_token(request.session, prepared.stored.id)
    stay = bool(user_text and guidance)
    return JSONResponse(
        {
            "ok": guide_error is None,
            "error": guide_error,
            "draft": guide_draft,
            "redirect": None if stay else redirect,
            "user": user_text,
            "guidance": guidance,
            "form_token": token,
        },
        status_code=200 if redirect or stay else status_code,
    )


def _recent_duplicate(db: Database, extraction_id: str, session_id: str, number: int, text: str) -> bool:
    latest = db.latest_cook_user_message(extraction_id, session_id, number)
    if latest is None or latest.body != text:
        return False
    age = (utcnow() - parse_iso(latest.created_at)).total_seconds()
    return 0 <= age <= DUPLICATE_WINDOW_SECONDS


@router.post("/recipes/{recipe_id}/cook/{step_token}/guide", response_class=HTMLResponse)
def cook_guide(
    request: Request,
    recipe_id: str,
    step_token: str,
    message: str = Form(default=""),
    quick_action: str = Form(default=""),
    form_token: str = Form(default=""),
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    prepared = prepare_recipe(db, recipe_id, session_id)
    _apply_scaled_display(db, prepared)
    if prepared is None:
        return not_found_response(request, settings)
    if prepared.pending:
        return _blocked(request, settings, prepared.stored.id)

    recipe_key = prepared.stored.id
    steps = prepared.working.steps
    try:
        number = parse_step_number(step_token)
    except CookNavigationError:
        return _invalid(request, settings, recipe_key, prepared.working.title)
    if not steps or number > len(steps):
        return _invalid(request, settings, recipe_key, prepared.working.title)

    if not consume_guide_token(request.session, recipe_key, form_token):
        cursor = cursor_for(request.session, recipe_key)
        if cursor.done or not steps:
            return RedirectResponse(url=f"/recipes/{recipe_key}/cook", status_code=303)
        index = resume_index(cursor, len(steps))
        return RedirectResponse(
            url=f"/recipes/{recipe_key}/cook/{index + 1}",
            status_code=303,
        )

    action_key = (quick_action or "").strip()
    if action_key:
        question = quick_action_text(action_key)
        if question is None:
            return _guide_client_response(
                request,
                settings,
                db,
                prepared,
                number,
                status_code=400,
                guide_error=INVALID_QUICK_MESSAGE,
                guide_draft=message,
            )
    else:
        question = message or ""

    command = navigation_command(question)
    if command is not None:
        try:
            target = apply_navigation(
                request.session, recipe_key, number, len(steps), command
            )
        except CookNavigationError:
            return _invalid(request, settings, recipe_key, prepared.working.title)
        return _guide_client_response(
            request,
            settings,
            db,
            prepared,
            number,
            redirect=f"/recipes/{recipe_key}{target}",
        )

    stripped = " ".join(question.split()).strip()
    if not stripped:
        return _guide_client_response(
            request,
            settings,
            db,
            prepared,
            number,
            status_code=400,
            guide_error=EMPTY_MESSAGE,
        )
    if len(stripped) > MAX_USER_CHARS:
        return _guide_client_response(
            request,
            settings,
            db,
            prepared,
            number,
            status_code=400,
            guide_error=OVERSIZE_MESSAGE,
            guide_draft=stripped,
        )

    if _recent_duplicate(db, recipe_key, session_id, number, stripped):
        return _guide_client_response(
            request,
            settings,
            db,
            prepared,
            number,
            redirect=None if _accepts_json(request) else f"/recipes/{recipe_key}/cook/{number}",
        )

    client = _guidance_client(request, settings)
    if client is None:
        return _guide_client_response(
            request,
            settings,
            db,
            prepared,
            number,
            status_code=503,
            guide_error=UNCONFIGURED_GUIDANCE,
            guide_draft=stripped,
        )

    limiter: ImportLimiter = request.app.state.guidance_limiter
    allowed = limiter.allow(
        session_id,
        per_client=settings.guidance_limit_per_session,
        per_process=settings.guidance_limit_per_process,
        window_seconds=settings.guidance_limit_window_seconds,
    )
    if not allowed:
        return _guide_client_response(
            request,
            settings,
            db,
            prepared,
            number,
            status_code=429,
            guide_error=RATE_LIMIT_MESSAGE,
            guide_draft=stripped,
        )

    current = [
        {"role": item.role, "text": item.body}
        for item in db.list_cook_messages(recipe_key, session_id, number)
    ]
    user_turns = sum(1 for item in current if item["role"] == "user")
    current.append({"role": "user", "text": stripped})
    earlier = []
    if user_turns < 2:
        earlier = [
            {
                "step_number": item.step_number,
                "role": item.role,
                "text": item.body,
            }
            for item in db.earlier_cook_messages(
                recipe_key,
                session_id,
                number,
                limit=MAX_EARLIER_MESSAGES,
            )
        ]
    context = build_model_context(
        prepared.working,
        step_number=number,
        current_messages=current,
        earlier_messages=earlier,
    )
    try:
        guidance = client.advise(context=context, settings=settings)
    except GuidanceError:
        return _guide_client_response(
            request,
            settings,
            db,
            prepared,
            number,
            status_code=502,
            guide_error=SAFE_GUIDANCE_ERROR,
            guide_draft=stripped,
        )
    except Exception:
        return _guide_client_response(
            request,
            settings,
            db,
            prepared,
            number,
            status_code=502,
            guide_error=SAFE_GUIDANCE_ERROR,
            guide_draft=stripped,
        )

    cleaned = " ".join(guidance.split()).strip()
    if not cleaned or len(cleaned) > MAX_GUIDANCE_CHARS:
        return _guide_client_response(
            request,
            settings,
            db,
            prepared,
            number,
            status_code=502,
            guide_error=SAFE_GUIDANCE_ERROR,
            guide_draft=stripped,
        )

    db.append_cook_exchange(
        extraction_id=recipe_key,
        session_id=session_id,
        step_number=number,
        user_text=stripped,
        assistant_text=cleaned,
        max_per_step=MAX_STORED_PER_STEP,
        max_per_recipe=MAX_STORED_PER_RECIPE,
    )
    return _guide_client_response(
        request,
        settings,
        db,
        prepared,
        number,
        user_text=stripped,
        guidance=cleaned,
        redirect=f"/recipes/{recipe_key}/cook/{number}",
    )
