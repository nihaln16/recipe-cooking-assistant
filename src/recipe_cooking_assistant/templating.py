from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from recipe_cooking_assistant.ingredient_display import format_ingredient_line

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"


def create_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["ingredient_line"] = format_ingredient_line
    return templates
