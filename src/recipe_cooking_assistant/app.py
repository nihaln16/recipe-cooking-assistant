from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from recipe_cooking_assistant.config import Settings, get_settings
from recipe_cooking_assistant.db import Database
from recipe_cooking_assistant.extraction import ExtractionClient
from recipe_cooking_assistant.import_limit import ImportLimiter
from recipe_cooking_assistant.routes.cook_routes import router as cook_router
from recipe_cooking_assistant.routes.health import router as health_router
from recipe_cooking_assistant.routes.import_routes import router as import_router
from recipe_cooking_assistant.routes.recipe_routes import router as recipe_router
from recipe_cooking_assistant.runtime import (
    assert_production_secrets,
    on_render,
    serve_host,
    serve_port,
)
from recipe_cooking_assistant.storage import delete_paths, ensure_upload_root

logger = logging.getLogger(__name__)

_PUBLIC_ERROR = (
    "<!DOCTYPE html><meta charset=\"utf-8\">"
    "<title>Something went wrong</title>"
    "<p>Something went wrong. Please try again.</p>"
)


PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"


def create_app(
    settings: Settings | None = None,
    *,
    extraction_client: ExtractionClient | None = None,
) -> FastAPI:
    assert_production_secrets()
    settings = settings or get_settings()
    db = Database(settings.database_path)
    db.init_schema()
    ensure_upload_root(settings)
    delete_paths(db.purge_expired())

    app = FastAPI(title=settings.app_name, debug=False)
    app.state.settings = settings
    app.state.db = db
    app.state.extraction_client = extraction_client
    app.state.import_limiter = ImportLimiter()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health_router)
    app.include_router(import_router)
    app.include_router(recipe_router)
    app.include_router(cook_router)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="rca_session",
        same_site="lax",
        https_only=on_render(),
        max_age=settings.session_ttl_hours * 60 * 60,
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        request: Request, exc: RequestValidationError
    ) -> HTMLResponse:
        _ = request, exc
        return HTMLResponse(_PUBLIC_ERROR, status_code=400)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> HTMLResponse:
        logger.error(
            "unhandled_error type=%s path=%s",
            type(exc).__name__,
            request.url.path,
        )
        return HTMLResponse(_PUBLIC_ERROR, status_code=500)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "recipe_cooking_assistant.app:app",
        host=serve_host(),
        port=serve_port(),
        reload=not on_render(),
        proxy_headers=on_render(),
    )
