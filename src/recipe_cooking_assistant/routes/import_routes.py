from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.db import Database
from recipe_cooking_assistant.deps import (
    get_db,
    get_session_id,
    get_settings,
)
from recipe_cooking_assistant.extraction import (
    SAFE_UI_API_ERROR,
    ExtractionError,
    get_extraction_client,
    log_extraction_failure,
)
from recipe_cooking_assistant.storage import UploadError, save_uploads
from recipe_cooking_assistant.templating import create_templates

templates = create_templates()

router = APIRouter(tags=["import"])


def _format_limits(settings: Settings) -> dict[str, str | int | float]:
    return {
        "max_images": settings.max_images,
        "max_mb": settings.max_upload_bytes / (1024 * 1024),
        "ttl_hours": settings.session_ttl_hours,
        "types_label": "JPEG, PNG, or WebP",
    }


def _import_context(
    settings: Settings,
    *,
    error: str | None = None,
    raw_text: str = "",
) -> dict:
    return {
        "app_name": settings.app_name,
        "limits": _format_limits(settings),
        "error": error,
        "raw_text": raw_text,
        "has_api_key": bool(settings.openai_api_key),
    }


@router.get("/", response_class=HTMLResponse)
def import_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    session_id: str = Depends(get_session_id),
) -> HTMLResponse:
    _ = session_id
    return templates.TemplateResponse(
        request,
        "import.html",
        _import_context(settings),
    )


@router.post("/import", response_class=HTMLResponse, response_model=None)
async def import_submit(
    request: Request,
    recipe_text: str = Form(default=""),
    images: list[UploadFile] | None = File(default=None),
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    text = recipe_text.strip() or None
    files = images or []

    if not text and not any(f.filename for f in files):
        return templates.TemplateResponse(
            request,
            "import.html",
            _import_context(
                settings,
                error="Paste recipe text and/or upload screenshots to continue.",
                raw_text=recipe_text,
            ),
            status_code=400,
        )

    bundle_id = str(uuid4())
    try:
        saved_images = await save_uploads(
            files=files,
            session_id=session_id,
            bundle_id=bundle_id,
            settings=settings,
        )
    except UploadError as exc:
        return templates.TemplateResponse(
            request,
            "import.html",
            _import_context(settings, error=exc.message, raw_text=recipe_text),
            status_code=400,
        )

    bundle = db.create_bundle(
        session_id=session_id,
        raw_text=text,
        settings=settings,
        images=saved_images,
        bundle_id=bundle_id,
    )

    try:
        extractor = request.app.state.extraction_client
        if extractor is None:
            extractor = get_extraction_client(settings)
        result, usage = extractor.extract(bundle=bundle, settings=settings)
    except ExtractionError as exc:
        return templates.TemplateResponse(
            request,
            "import.html",
            _import_context(settings, error=exc.message, raw_text=recipe_text),
            status_code=503,
        )
    except Exception as exc:
        log_extraction_failure(exc, bundle_id=bundle.id)
        return templates.TemplateResponse(
            request,
            "import.html",
            _import_context(
                settings,
                error=SAFE_UI_API_ERROR,
                raw_text=recipe_text,
            ),
            status_code=502,
        )

    stored = db.save_extraction(
        bundle=bundle,
        result=result,
        usage=usage,
        settings=settings,
    )

    if result.insufficient_source:
        return templates.TemplateResponse(
            request,
            "import.html",
            _import_context(
                settings,
                error=(
                    result.insufficient_reason
                    or "Add readable recipe text or screenshots. "
                    "A finished-dish photo alone is not enough."
                ),
                raw_text=recipe_text,
            ),
            status_code=422,
        )

    return RedirectResponse(url=f"/recipes/{stored.id}", status_code=303)


@router.get("/sources/{bundle_id}", response_class=HTMLResponse)
def source_detail(
    request: Request,
    bundle_id: str,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    session_id: str = Depends(get_session_id),
) -> HTMLResponse:
    bundle = db.get_bundle_for_session(bundle_id, session_id)
    if bundle is None:
        return templates.TemplateResponse(
            request,
            "import.html",
            _import_context(
                settings,
                error="That recipe source was not found, expired, or belongs to another session.",
            ),
            status_code=404,
        )

    return templates.TemplateResponse(
        request,
        "source_saved.html",
        {
            "app_name": settings.app_name,
            "bundle": bundle,
            "limits": _format_limits(settings),
        },
    )
