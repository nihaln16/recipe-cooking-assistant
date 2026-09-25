"""Manual edits, preparation checklist, and ingredient substitution chat."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.cooking import (
    SUBSTITUTION_TOKEN_KEY,
    consume_guide_token,
    ensure_guide_token,
)
from recipe_cooking_assistant.db import Database, parse_iso, utcnow
from recipe_cooking_assistant.deps import get_db, get_session_id, get_settings
from recipe_cooking_assistant.edits import EditError, apply_edit_action
from recipe_cooking_assistant.guidance import (
    DUPLICATE_WINDOW_SECONDS,
    EMPTY_MESSAGE,
    suggestion_names,
    MAX_GUIDANCE_CHARS,
    MAX_STORED_PER_RECIPE,
    MAX_STORED_PER_STEP,
    MAX_USER_CHARS,
    OVERSIZE_MESSAGE,
    RATE_LIMIT_MESSAGE,
    SAFE_GUIDANCE_ERROR,
    UNCONFIGURED_GUIDANCE,
    GuidanceError,
    OpenAIGuidanceClient,
    build_substitution_context,
)
from recipe_cooking_assistant.import_limit import ImportLimiter
from recipe_cooking_assistant.ingredient_display import group_listed_ingredients
from recipe_cooking_assistant.models import ExtractedIngredient
from recipe_cooking_assistant.routes.recipe_routes import (
    PreparedRecipe,
    not_found_response,
    prepare_recipe,
)
from recipe_cooking_assistant.scaling import display_recipe, guidance_note_text, working_fingerprint
from recipe_cooking_assistant.templating import create_templates

templates = create_templates()
router = APIRouter(tags=["customize"])

REVIEW_FIRST = (
    "Finish every review item before editing this recipe. "
    "That keeps review decisions and direct edits from conflicting."
)
SUGGEST_QUESTION = "What can I use instead of this ingredient?"


def _guidance_client(request: Request, settings: Settings):
    client = request.app.state.guidance_client
    if client is not None:
        return client
    if not settings.openai_api_key:
        return None
    return OpenAIGuidanceClient(settings.openai_api_key)


def _edit_page(
    request: Request,
    settings: Settings,
    prepared: PreparedRecipe,
    *,
    status_code: int = 200,
    edit_error: str | None = None,
    draft: dict | None = None,
    focus_ingredient: ExtractedIngredient | None = None,
) -> HTMLResponse:
    reviewed_ids = {item.id for item in prepared.reviewed.ingredients}
    removed = [
        item
        for item in prepared.reviewed.ingredients
        if item.id in prepared.edits.removed_ingredient_ids and item.list_status == "listed"
    ]
    removed_steps = [
        item
        for item in prepared.reviewed.steps
        if item.id in prepared.edits.removed_step_ids
    ]
    return templates.TemplateResponse(
        request,
        "edit.html",
        {
            "app_name": settings.app_name,
            "stored": prepared.stored,
            "recipe": prepared.working,
            "reviewed": prepared.reviewed,
            "edits": prepared.edits,
            "ingredient_groups": group_listed_ingredients(prepared.working.ingredients),
            "removed_ingredients": removed,
            "removed_steps": removed_steps,
            "reviewed_ingredient_ids": reviewed_ids,
            "edit_error": edit_error,
            "draft": draft or {},
            "can_edit": not prepared.pending,
            "focus_ingredient": focus_ingredient,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/recipes/{recipe_id}/edit", response_class=HTMLResponse)
def edit_page(
    request: Request,
    recipe_id: str,
    ingredient: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
) -> HTMLResponse:
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)
    if prepared.pending:
        return _edit_page(
            request, settings, prepared, status_code=400, edit_error=REVIEW_FIRST
        )
    focus = None
    error = None
    if ingredient:
        focus = next(
            (
                item
                for item in prepared.working.ingredients
                if item.id == ingredient and item.list_status == "listed"
            ),
            None,
        )
        if focus is None:
            error = "That ingredient is not on the working recipe."
    return _edit_page(
        request, settings, prepared, edit_error=error, focus_ingredient=focus
    )


@router.post("/recipes/{recipe_id}/edit", response_class=HTMLResponse)
def edit_submit(
    request: Request,
    recipe_id: str,
    action: str = Form(default=""),
    title: str | None = Form(default=None),
    servings: str | None = Form(default=None),
    ingredient_id: str | None = Form(default=None),
    step_id: str | None = Form(default=None),
    name: str | None = Form(default=None),
    quantity: str | None = Form(default=None),
    unit: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    text: str | None = Form(default=None),
    optional: str | None = Form(default=None),
    direction: str | None = Form(default=None),
    focus_ingredient: str | None = Form(default=None),
    return_to: str | None = Form(default=None),
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)
    if prepared.pending:
        return _edit_page(
            request, settings, prepared, status_code=400, edit_error=REVIEW_FIRST
        )
    form = {
        "title": title,
        "servings": servings,
        "ingredient_id": ingredient_id,
        "step_id": step_id,
        "name": name,
        "quantity": quantity,
        "unit": unit,
        "notes": notes,
        "text": text,
        "optional": optional,
        "direction": direction,
    }
    try:
        updated = apply_edit_action(prepared.reviewed, prepared.edits, action, form)
    except EditError as exc:
        return _edit_page(
            request,
            settings,
            prepared,
            status_code=400,
            edit_error=exc.message,
            draft=form,
        )
    db.save_recipe_edits(
        extraction_id=prepared.stored.id,
        session_id=session_id,
        edits=updated,
    )
    if action == "save_ingredient" and (return_to or "").strip() == "prepare":
        return RedirectResponse(
            url=f"/recipes/{prepared.stored.id}/prepare",
            status_code=303,
        )
    target = f"/recipes/{prepared.stored.id}/edit"
    chosen = (focus_ingredient or "").strip()
    if chosen:
        target = f"{target}?ingredient={chosen}"
    return RedirectResponse(url=target, status_code=303)


def _prepare_page(
    request: Request,
    settings: Settings,
    prepared: PreparedRecipe,
    db: Database,
    *,
    status_code: int = 200,
    prep_error: str | None = None,
) -> HTMLResponse:
    checked = db.list_prep_checks(prepared.stored.id, prepared.stored.session_id)
    listed_ids = {item.id for item in prepared.working.listed_ingredients()}
    view = db.get_scaled_view(prepared.stored.id, prepared.stored.session_id)
    if view and view.get("fingerprint") != working_fingerprint(prepared.working):
        view = None
    shown = display_recipe(prepared.working, view)
    return templates.TemplateResponse(
        request,
        "prepare.html",
        {
            "app_name": settings.app_name,
            "stored": prepared.stored,
            "recipe": shown,
            "ingredient_groups": group_listed_ingredients(shown.ingredients),
            "checked_ids": checked & listed_ids,
            "prep_error": prep_error,
            "guidance_note": guidance_note_text(view) if view and view.get("quantities_confirmed") else "",
            "guidance_provenance": (view or {}).get("guidance_provenance"),
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/recipes/{recipe_id}/prepare", response_class=HTMLResponse)
def prepare_page(
    request: Request,
    recipe_id: str,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
) -> HTMLResponse:
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)
    if prepared.pending:
        return _prepare_page(
            request, settings, prepared, db, status_code=400, prep_error=REVIEW_FIRST
        )
    return _prepare_page(request, settings, prepared, db)


@router.post("/recipes/{recipe_id}/prepare", response_class=HTMLResponse)
def prepare_submit(
    request: Request,
    recipe_id: str,
    ingredient_id: str = Form(default=""),
    checked: str = Form(default=""),
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)
    if prepared.pending:
        return _prepare_page(
            request, settings, prepared, db, status_code=400, prep_error=REVIEW_FIRST
        )
    ingredient_id = ingredient_id.strip()
    listed = {item.id for item in prepared.working.listed_ingredients()}
    if ingredient_id not in listed or checked not in {"0", "1"}:
        return _prepare_page(
            request,
            settings,
            prepared,
            db,
            status_code=400,
            prep_error="That ingredient is not on the working recipe.",
        )
    is_checked = checked == "1"
    db.set_prep_check(
        extraction_id=prepared.stored.id,
        session_id=session_id,
        ingredient_id=ingredient_id,
        checked=is_checked,
    )
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(
            {"ok": True, "ingredient_id": ingredient_id, "checked": is_checked}
        )
    return RedirectResponse(
        url=f"/recipes/{prepared.stored.id}/prepare", status_code=303
    )


def _target(prepared: PreparedRecipe, ingredient_id: str) -> ExtractedIngredient | None:
    return next(
        (
            item
            for item in prepared.working.listed_ingredients()
            if item.id == ingredient_id
        ),
        None,
    )


def _alternatives(prepared: PreparedRecipe, target: ExtractedIngredient):
    if not target.alternative_group_id:
        return []
    return [
        item
        for item in prepared.working.listed_ingredients()
        if item.alternative_group_id == target.alternative_group_id
        and item.id != target.id
    ]


def _substitute_page(
    request: Request,
    settings: Settings,
    db: Database,
    prepared: PreparedRecipe,
    target: ExtractedIngredient,
    *,
    status_code: int = 200,
    guide_error: str | None = None,
    guide_draft: str = "",
) -> HTMLResponse:
    messages = db.list_substitution_messages(
        prepared.stored.id, prepared.stored.session_id, target.id
    )
    latest = next(
        (item.body for item in reversed(messages) if item.role == "assistant"),
        "",
    )
    return templates.TemplateResponse(
        request,
        "substitute.html",
        {
            "app_name": settings.app_name,
            "stored": prepared.stored,
            "recipe": prepared.working,
            "target": target,
            "alternatives": _alternatives(prepared, target),
            "messages": messages,
            "suggestions": suggestion_names(latest),
            "form_token": ensure_guide_token(
                request.session, prepared.stored.id, SUBSTITUTION_TOKEN_KEY
            ),
            "guide_error": guide_error,
            "guide_draft": guide_draft,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/recipes/{recipe_id}/substitute/{ingredient_id}", response_class=HTMLResponse
)
def substitute_page(
    request: Request,
    recipe_id: str,
    ingredient_id: str,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
) -> HTMLResponse:
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)
    if prepared.pending:
        return templates.TemplateResponse(
            request,
            "substitute.html",
            {
                "app_name": settings.app_name,
                "stored": prepared.stored,
                "recipe": prepared.working,
                "blocked": REVIEW_FIRST,
                "target": None,
            },
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    target = _target(prepared, ingredient_id)
    if target is None:
        return templates.TemplateResponse(
            request,
            "substitute.html",
            {
                "app_name": settings.app_name,
                "stored": prepared.stored,
                "recipe": prepared.working,
                "blocked": "That ingredient is not on the working recipe.",
                "target": None,
            },
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    return _substitute_page(request, settings, db, prepared, target)


def _recent_duplicate(
    db: Database, extraction_id: str, session_id: str, ingredient_id: str, text: str
) -> bool:
    latest = db.latest_substitution_user_message(extraction_id, session_id, ingredient_id)
    if latest is None or latest.body != text:
        return False
    age = (utcnow() - parse_iso(latest.created_at)).total_seconds()
    return 0 <= age <= DUPLICATE_WINDOW_SECONDS


@router.post(
    "/recipes/{recipe_id}/substitute/{ingredient_id}", response_class=HTMLResponse
)
def substitute_submit(
    request: Request,
    recipe_id: str,
    ingredient_id: str,
    message: str = Form(default=""),
    quick_action: str = Form(default=""),
    form_token: str = Form(default=""),
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)
    if prepared.pending:
        return templates.TemplateResponse(
            request,
            "substitute.html",
            {
                "app_name": settings.app_name,
                "stored": prepared.stored,
                "recipe": prepared.working,
                "blocked": REVIEW_FIRST,
                "target": None,
            },
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    target = _target(prepared, ingredient_id)
    if target is None:
        return templates.TemplateResponse(
            request,
            "substitute.html",
            {
                "app_name": settings.app_name,
                "stored": prepared.stored,
                "recipe": prepared.working,
                "blocked": "That ingredient is not on the working recipe.",
                "target": None,
            },
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    if not consume_guide_token(
        request.session, prepared.stored.id, form_token, SUBSTITUTION_TOKEN_KEY
    ):
        return RedirectResponse(
            url=f"/recipes/{prepared.stored.id}/substitute/{target.id}",
            status_code=303,
        )
    action_key = (quick_action or "").strip()
    if action_key == "suggest":
        question = SUGGEST_QUESTION
    elif action_key:
        return _substitute_page(
            request,
            settings,
            db,
            prepared,
            target,
            status_code=400,
            guide_error="Choose a substitution question or type one.",
            guide_draft=message,
        )
    else:
        question = message or ""
    stripped = " ".join(question.split()).strip()
    if not stripped:
        return _substitute_page(
            request,
            settings,
            db,
            prepared,
            target,
            status_code=400,
            guide_error=EMPTY_MESSAGE,
        )
    if len(stripped) > MAX_USER_CHARS:
        return _substitute_page(
            request,
            settings,
            db,
            prepared,
            target,
            status_code=400,
            guide_error=OVERSIZE_MESSAGE,
            guide_draft=stripped,
        )
    if _recent_duplicate(db, prepared.stored.id, session_id, target.id, stripped):
        return RedirectResponse(
            url=f"/recipes/{prepared.stored.id}/substitute/{target.id}",
            status_code=303,
        )
    client = _guidance_client(request, settings)
    if client is None:
        return _substitute_page(
            request,
            settings,
            db,
            prepared,
            target,
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
        return _substitute_page(
            request,
            settings,
            db,
            prepared,
            target,
            status_code=429,
            guide_error=RATE_LIMIT_MESSAGE,
            guide_draft=stripped,
        )
    history = [
        {"role": item.role, "text": item.body}
        for item in db.list_substitution_messages(
            prepared.stored.id, session_id, target.id
        )
    ]
    history.append({"role": "user", "text": stripped})
    context = build_substitution_context(
        prepared.working,
        ingredient_id=target.id,
        current_messages=history,
    )
    try:
        guidance = client.advise(context=context, settings=settings)
    except GuidanceError:
        return _substitute_page(
            request,
            settings,
            db,
            prepared,
            target,
            status_code=502,
            guide_error=SAFE_GUIDANCE_ERROR,
            guide_draft=stripped,
        )
    except Exception:
        return _substitute_page(
            request,
            settings,
            db,
            prepared,
            target,
            status_code=502,
            guide_error=SAFE_GUIDANCE_ERROR,
            guide_draft=stripped,
        )
    cleaned = " ".join(guidance.split()).strip()
    if not cleaned or len(cleaned) > MAX_GUIDANCE_CHARS:
        return _substitute_page(
            request,
            settings,
            db,
            prepared,
            target,
            status_code=502,
            guide_error=SAFE_GUIDANCE_ERROR,
            guide_draft=stripped,
        )
    db.append_substitution_exchange(
        extraction_id=prepared.stored.id,
        session_id=session_id,
        ingredient_id=target.id,
        user_text=stripped,
        assistant_text=cleaned,
        max_per_ingredient=MAX_STORED_PER_STEP,
        max_per_recipe=MAX_STORED_PER_RECIPE,
    )
    return RedirectResponse(
        url=f"/recipes/{prepared.stored.id}/substitute/{target.id}",
        status_code=303,
    )
