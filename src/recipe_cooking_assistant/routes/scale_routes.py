"""Scaled cooking view. Does not change the working recipe."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.db import Database
from recipe_cooking_assistant.deps import get_db, get_session_id, get_settings
from recipe_cooking_assistant.guidance import (
    RATE_LIMIT_MESSAGE,
    SAFE_SCALING_ERROR,
    UNCONFIGURED_GUIDANCE,
    GuidanceError,
    OpenAIGuidanceClient,
    build_scaling_context,
)
from recipe_cooking_assistant.import_limit import ImportLimiter
from recipe_cooking_assistant.routes.recipe_routes import not_found_response, prepare_recipe
from recipe_cooking_assistant.scaling import (
    ScaleError,
    build_scaled_view,
    double_target,
    guidance_note_text,
    working_fingerprint,
)
from recipe_cooking_assistant.templating import create_templates

templates = create_templates()
router = APIRouter(tags=["scale"])

REVIEW_FIRST = "Finish every review item before scaling this recipe."


def _client(request: Request, settings: Settings):
    client = request.app.state.guidance_client
    if client is not None:
        return client
    if not settings.openai_api_key:
        return None
    return OpenAIGuidanceClient(settings.openai_api_key)


def _fresh_view(prepared, stored: dict | None) -> dict | None:
    if stored is None:
        return None
    if stored.get("fingerprint") != working_fingerprint(prepared.working):
        stored = dict(stored)
        stored["proposals"] = []
        stored["confirmed"] = []
        try:
            rebuilt = build_scaled_view(prepared.working, str(stored.get("target") or ""))
        except ScaleError:
            return None
        rebuilt["confirmed"] = []
        return rebuilt
    return stored


def _page(request, settings, prepared, db, *, status_code=200, error=None):
    stored = _fresh_view(prepared, db.get_scaled_view(prepared.stored.id, prepared.stored.session_id))
    return templates.TemplateResponse(
        request,
        "scale.html",
        {
            "app_name": settings.app_name,
            "stored": prepared.stored,
            "recipe": prepared.working,
            "view": stored,
            "guidance_note": guidance_note_text(stored),
            "scale_error": error,
            "can_scale": not prepared.pending,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/recipes/{recipe_id}/scale", response_class=HTMLResponse)
def scale_page(
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
        return _page(request, settings, prepared, db, status_code=400, error=REVIEW_FIRST)
    return _page(request, settings, prepared, db)


@router.post("/recipes/{recipe_id}/scale", response_class=HTMLResponse)
def scale_submit(
    request: Request,
    recipe_id: str,
    action: str = Form(default="set"),
    target: str = Form(default=""),
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)
    if prepared.pending:
        return _page(request, settings, prepared, db, status_code=400, error=REVIEW_FIRST)
    try:
        chosen = double_target(prepared.working.servings) if action == "double" else target
        view = build_scaled_view(prepared.working, chosen)
    except ScaleError as exc:
        return _page(request, settings, prepared, db, status_code=400, error=exc.message)
    db.save_scaled_view(extraction_id=prepared.stored.id, session_id=session_id, view=view)
    return RedirectResponse(url=f"/recipes/{prepared.stored.id}/scale", status_code=303)


@router.post("/recipes/{recipe_id}/scale/guide", response_class=HTMLResponse)
def scale_guide(
    request: Request,
    recipe_id: str,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)
    view = _fresh_view(prepared, db.get_scaled_view(prepared.stored.id, session_id))
    if view is None:
        return _page(
            request,
            settings,
            prepared,
            db,
            status_code=400,
            error="Choose a target serving count before asking for scaling guidance.",
        )
    if guidance_note_text(view):
        if not view.get("guidance_note"):
            view["guidance_note"] = guidance_note_text(view)
            view["guidance_provenance"] = view.get("guidance_provenance") or "ai_suggestion"
            db.save_scaled_view(extraction_id=prepared.stored.id, session_id=session_id, view=view)
        return RedirectResponse(url=f"/recipes/{prepared.stored.id}/scale", status_code=303)
    client = _client(request, settings)
    if client is None or not hasattr(client, "advise_scaling"):
        return _page(
            request, settings, prepared, db, status_code=503, error=UNCONFIGURED_GUIDANCE
        )
    limiter: ImportLimiter = request.app.state.guidance_limiter
    if not limiter.allow(
        session_id,
        per_client=settings.guidance_limit_per_session,
        per_process=settings.guidance_limit_per_process,
        window_seconds=settings.guidance_limit_window_seconds,
    ):
        return _page(request, settings, prepared, db, status_code=429, error=RATE_LIMIT_MESSAGE)
    context = build_scaling_context(prepared.working, view)
    try:
        note = client.advise_scaling(context=context, settings=settings)
    except GuidanceError:
        return _page(request, settings, prepared, db, status_code=502, error=SAFE_SCALING_ERROR)
    except Exception:
        return _page(request, settings, prepared, db, status_code=502, error=SAFE_SCALING_ERROR)
    view["guidance_note"] = note
    view["guidance_provenance"] = "ai_suggestion"
    view["proposals"] = []
    db.save_scaled_view(extraction_id=prepared.stored.id, session_id=session_id, view=view)
    return RedirectResponse(url=f"/recipes/{prepared.stored.id}/scale", status_code=303)


@router.post("/recipes/{recipe_id}/scale/confirm", response_class=HTMLResponse)
def scale_confirm(
    request: Request,
    recipe_id: str,
    suggestion: str = Form(default=""),
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)
    view = db.get_scaled_view(prepared.stored.id, session_id)
    if view is None:
        return _page(request, settings, prepared, db, status_code=400, error="There is no scaled view to confirm.")
    original = guidance_note_text(view)
    text = " ".join(suggestion.split()).strip()
    view["guidance_note"] = text
    if text and text != original:
        view["guidance_provenance"] = "user_edit"
    elif text:
        view["guidance_provenance"] = view.get("guidance_provenance") or "ai_suggestion"
    view["quantities_confirmed"] = True
    view["proposals"] = []
    db.save_scaled_view(extraction_id=prepared.stored.id, session_id=session_id, view=view)
    return RedirectResponse(url=f"/recipes/{prepared.stored.id}/prepare", status_code=303)


@router.post("/recipes/{recipe_id}/scale/rollback", response_class=HTMLResponse)
def scale_rollback(
    request: Request,
    recipe_id: str,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)
    db.delete_scaled_view(prepared.stored.id, session_id)
    return RedirectResponse(url=f"/recipes/{prepared.stored.id}/scale", status_code=303)
