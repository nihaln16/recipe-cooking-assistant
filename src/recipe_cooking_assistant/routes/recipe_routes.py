from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.db import Database
from recipe_cooking_assistant.deps import get_db, get_session_id, get_settings
from recipe_cooking_assistant.ingredient_display import group_listed_ingredients
from recipe_cooking_assistant.models import StoredExtraction
from recipe_cooking_assistant.normalize import canonicalize_findings, remap_review_decisions
from recipe_cooking_assistant.review import (
    ReviewError,
    apply_decisions,
    build_review_cards,
    parse_review_action,
)
from recipe_cooking_assistant.templating import create_templates

templates = create_templates()

router = APIRouter(tags=["recipes"])


def _not_found_context(settings: Settings) -> dict:
    return {
        "app_name": settings.app_name,
        "limits": {
            "max_images": settings.max_images,
            "max_mb": settings.max_upload_bytes / (1024 * 1024),
            "ttl_hours": settings.session_ttl_hours,
            "types_label": "JPEG, PNG, or WebP",
        },
        "error": "That recipe was not found, expired, or belongs to another session.",
        "raw_text": "",
        "has_api_key": bool(settings.openai_api_key),
    }


def _recipe_context(
    *,
    settings: Settings,
    stored: StoredExtraction,
    bundle,
    decisions,
    review_error: str | None = None,
    open_edit: str | None = None,
) -> dict:
    canonicalize_findings(stored.result)
    decisions = remap_review_decisions(decisions, stored.result)
    working = apply_decisions(stored.result, decisions)
    cards = build_review_cards(stored.result, working, decisions)
    return {
        "app_name": settings.app_name,
        "stored": stored,
        "recipe": working,
        "usage": stored.usage,
        "bundle": bundle,
        "ingredient_groups": group_listed_ingredients(working.ingredients),
        "pending_review": [card for card in cards if card.decision is None],
        "decided_review": [card for card in cards if card.decision is not None],
        "review_error": review_error,
        "open_edit": open_edit,
    }


@router.get("/recipes/{recipe_id}", response_class=HTMLResponse)
def recipe_detail(
    request: Request,
    recipe_id: str,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
) -> HTMLResponse:
    stored = db.get_extraction_for_session(recipe_id, session_id)
    if stored is None:
        return templates.TemplateResponse(
            request,
            "import.html",
            _not_found_context(settings),
            status_code=404,
        )

    bundle = db.get_bundle_for_session(stored.bundle_id, session_id)
    decisions = db.list_review_decisions(stored.id, session_id)
    return templates.TemplateResponse(
        request,
        "recipe.html",
        _recipe_context(
            settings=settings,
            stored=stored,
            bundle=bundle,
            decisions=decisions,
        ),
    )


@router.post("/recipes/{recipe_id}/review/{finding_id}", response_class=HTMLResponse)
def review_finding(
    request: Request,
    recipe_id: str,
    finding_id: str,
    action: str = Form(default=""),
    name: str | None = Form(default=None),
    quantity: str | None = Form(default=None),
    unit: str | None = Form(default=None),
    text: str | None = Form(default=None),
    choice: str | None = Form(default=None),
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    stored = db.get_extraction_for_session(recipe_id, session_id)
    if stored is None:
        return templates.TemplateResponse(
            request,
            "import.html",
            _not_found_context(settings),
            status_code=404,
        )

    bundle = db.get_bundle_for_session(stored.bundle_id, session_id)
    canonicalize_findings(stored.result)
    try:
        decision = parse_review_action(
            result=stored.result,
            finding_id=finding_id,
            action=action,
            form={
                "name": name,
                "quantity": quantity,
                "unit": unit,
                "text": text,
                "choice": choice,
            },
        )
    except ReviewError as exc:
        decisions = db.list_review_decisions(stored.id, session_id)
        return templates.TemplateResponse(
            request,
            "recipe.html",
            _recipe_context(
                settings=settings,
                stored=stored,
                bundle=bundle,
                decisions=decisions,
                review_error=exc.message,
                open_edit=finding_id if action == "edit" else None,
            ),
            status_code=400,
        )

    db.upsert_review_decision(
        extraction_id=stored.id,
        session_id=session_id,
        decision=decision,
    )
    return RedirectResponse(url=f"/recipes/{recipe_id}", status_code=303)
