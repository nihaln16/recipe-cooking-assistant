from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.db import Database
from recipe_cooking_assistant.deps import get_db, get_session_id, get_settings
from recipe_cooking_assistant.ingredient_display import group_listed_ingredients
from recipe_cooking_assistant.templating import create_templates

templates = create_templates()

router = APIRouter(tags=["recipes"])


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
            {
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
            },
            status_code=404,
        )

    bundle = db.get_bundle_for_session(stored.bundle_id, session_id)
    return templates.TemplateResponse(
        request,
        "recipe.html",
        {
            "app_name": settings.app_name,
            "stored": stored,
            "recipe": stored.result,
            "usage": stored.usage,
            "bundle": bundle,
            "ingredient_groups": group_listed_ingredients(stored.result.ingredients),
        },
    )
