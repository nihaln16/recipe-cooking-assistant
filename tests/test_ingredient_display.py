from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from recipe_cooking_assistant.app import create_app
from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.extraction import StaticExtractionClient, normalize_result
from recipe_cooking_assistant.ingredient_display import (
    format_ingredient_line,
    normalize_ingredient_qualifiers,
)
from recipe_cooking_assistant.models import (
    ExtractedIngredient,
    ExtractedStep,
    ExtractionResult,
    SourceEvidence,
)

SOURCE_PHRASE = "salt and ground black pepper to taste"


def test_to_taste_moved_from_quantity_to_notes_and_formatted() -> None:
    """Regression: model often puts 'to taste' in quantity → 'to taste salt'."""
    assert SOURCE_PHRASE == "salt and ground black pepper to taste"

    salt = ExtractedIngredient(
        id="ing_1",
        name="salt",
        quantity="to taste",
        unit=None,
        notes=None,
        evidence=SourceEvidence(quote=SOURCE_PHRASE),
    )
    pepper = ExtractedIngredient(
        id="ing_2",
        name="ground black pepper",
        quantity="to taste",
        unit=None,
        notes=None,
        evidence=SourceEvidence(quote=SOURCE_PHRASE),
    )

    normalize_ingredient_qualifiers(salt)
    normalize_ingredient_qualifiers(pepper)

    assert salt.quantity is None
    assert salt.notes == "to taste"
    assert pepper.quantity is None
    assert pepper.notes == "to taste"
    assert format_ingredient_line(salt) == "Salt — to taste"
    assert format_ingredient_line(pepper) == "Ground black pepper — to taste"


def test_normalize_result_applies_qualifier_fix() -> None:
    result = ExtractionResult(
        title="Seasoning check",
        ingredients=[
            ExtractedIngredient(
                id="ing_1",
                name="salt",
                quantity="to taste",
                evidence=SourceEvidence(quote=SOURCE_PHRASE),
            ),
            ExtractedIngredient(
                id="ing_2",
                name="ground black pepper",
                quantity="to taste",
                evidence=SourceEvidence(quote=SOURCE_PHRASE),
            ),
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Season and serve.",
                evidence=SourceEvidence(quote="Season and serve."),
            )
        ],
    )
    normalized = normalize_result(result, [])
    lines = [format_ingredient_line(i) for i in normalized.ingredients]
    assert lines == ["Salt — to taste", "Ground black pepper — to taste"]


def test_recipe_page_renders_to_taste_with_em_dash(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Pepper test",
        ingredients=[
            ExtractedIngredient(
                id="ing_1",
                name="salt",
                quantity="to taste",
                evidence=SourceEvidence(quote=SOURCE_PHRASE),
            ),
            ExtractedIngredient(
                id="ing_2",
                name="ground black pepper",
                quantity="to taste",
                evidence=SourceEvidence(quote=SOURCE_PHRASE),
            ),
        ],
        steps=[
            ExtractedStep(id="step_1", text="Season.", confidence="high")
        ],
    )
    settings = Settings(
        session_secret="test-secret",
        data_dir=tmp_path,
        openai_api_key=None,
    )
    app = create_app(
        settings,
        extraction_client=StaticExtractionClient(result),
    )
    with TestClient(app) as client:
        response = client.post(
            "/import",
            data={"recipe_text": SOURCE_PHRASE + "\nSeason and serve."},
            follow_redirects=True,
        )
    assert response.status_code == 200
    assert "Salt — to taste" in response.text
    assert "Ground black pepper — to taste" in response.text
    assert "to taste salt" not in response.text.lower()
    assert SOURCE_PHRASE in response.text
