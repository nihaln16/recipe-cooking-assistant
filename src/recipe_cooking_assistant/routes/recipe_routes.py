from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.db import Database, SourceBundle
from recipe_cooking_assistant.deps import get_db, get_session_id, get_settings
from recipe_cooking_assistant.edits import RecipeEdits, apply_direct_edits
from recipe_cooking_assistant.ingredient_display import group_listed_ingredients
from recipe_cooking_assistant.models import ExtractionResult, ReviewDecision, StoredExtraction
from recipe_cooking_assistant.normalize import canonicalize_findings, remap_review_decisions
from recipe_cooking_assistant.review import (
    ReviewCard,
    ReviewError,
    apply_decisions,
    build_review_cards,
    parse_review_action,
)
from recipe_cooking_assistant.templating import create_templates

templates = create_templates()

router = APIRouter(tags=["recipes"])


@dataclass
class PreparedRecipe:
    stored: StoredExtraction
    bundle: SourceBundle | None
    decisions: list[ReviewDecision]
    reviewed: ExtractionResult
    edits: RecipeEdits
    working: ExtractionResult
    cards: list[ReviewCard]

    @property
    def pending(self) -> list[ReviewCard]:
        return [card for card in self.cards if card.decision is None]

    @property
    def decided(self) -> list[ReviewCard]:
        return [card for card in self.cards if card.decision is not None]


def prepare_recipe(
    db: Database, recipe_id: str, session_id: str
) -> PreparedRecipe | None:
    """Load a session-owned recipe and derive the working copy.

    Canonicalizes findings in memory, remaps review decisions, and applies
    accepted and edited decisions. Does not write the extraction payload.
    """
    stored = db.get_extraction_for_session(recipe_id, session_id)
    if stored is None:
        return None
    bundle = db.get_bundle_for_session(stored.bundle_id, session_id)
    canonicalize_findings(stored.result)
    decisions = remap_review_decisions(
        db.list_review_decisions(stored.id, session_id),
        stored.result,
    )
    reviewed = apply_decisions(stored.result, decisions)
    edits = db.get_recipe_edits(stored.id, session_id)
    working = apply_direct_edits(reviewed, edits)
    cards = build_review_cards(stored.result, working, decisions)
    return PreparedRecipe(
        stored=stored,
        bundle=bundle,
        decisions=decisions,
        reviewed=reviewed,
        edits=edits,
        working=working,
        cards=cards,
    )


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


def not_found_response(request: Request, settings: Settings) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "import.html",
        _not_found_context(settings),
        status_code=404,
        headers={"Cache-Control": "no-store"},
    )


def _recipe_context(
    *,
    settings: Settings,
    prepared: PreparedRecipe,
    review_error: str | None = None,
    open_edit: str | None = None,
) -> dict:
    working = prepared.working
    return {
        "app_name": settings.app_name,
        "stored": prepared.stored,
        "recipe": working,
        "usage": prepared.stored.usage,
        "bundle": prepared.bundle,
        "ingredient_groups": group_listed_ingredients(working.ingredients),
        "pending_review": prepared.pending,
        "decided_review": prepared.decided,
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
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)

    return templates.TemplateResponse(
        request,
        "recipe.html",
        _recipe_context(settings=settings, prepared=prepared),
        headers={"Cache-Control": "no-store"},
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
    prepared = prepare_recipe(db, recipe_id, session_id)
    if prepared is None:
        return not_found_response(request, settings)

    try:
        decision = parse_review_action(
            result=prepared.stored.result,
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
        return templates.TemplateResponse(
            request,
            "recipe.html",
            _recipe_context(
                settings=settings,
                prepared=prepared,
                review_error=exc.message,
                open_edit=finding_id if action == "edit" else None,
            ),
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    db.upsert_review_decision(
        extraction_id=prepared.stored.id,
        session_id=session_id,
        decision=decision,
    )
    return RedirectResponse(url=f"/recipes/{recipe_id}", status_code=303)
