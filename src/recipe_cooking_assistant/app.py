from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from recipe_cooking_assistant.config import Settings, get_settings
from recipe_cooking_assistant.db import Database
from recipe_cooking_assistant.extraction import ExtractionClient
from recipe_cooking_assistant.routes.cook_routes import router as cook_router
from recipe_cooking_assistant.routes.health import router as health_router
from recipe_cooking_assistant.routes.import_routes import router as import_router
from recipe_cooking_assistant.routes.recipe_routes import router as recipe_router
from recipe_cooking_assistant.storage import delete_paths, ensure_upload_root


PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"


def create_app(
    settings: Settings | None = None,
    *,
    extraction_client: ExtractionClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.database_path)
    db.init_schema()
    ensure_upload_root(settings)
    delete_paths(db.purge_expired())

    app = FastAPI(title=settings.app_name)
    app.state.settings = settings
    app.state.db = db
    app.state.extraction_client = extraction_client

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
        https_only=False,
        max_age=settings.session_ttl_hours * 60 * 60,
    )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "recipe_cooking_assistant.app:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
