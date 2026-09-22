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


def test_user_edited_quantity_overrides_source_text() -> None:
    item = ExtractedIngredient(
        id="ing_oil",
        name="olive oil",
        quantity="1",
        unit="tablespoon",
        source_text="Olive oil",
        quantity_provenance="user_edit",
        unit_provenance="user_edit",
    )
    assert format_ingredient_line(item) == "1 tablespoon olive oil"
    assert item.source_text == "Olive oil"
    assert item.quantity_provenance == "user_edit"


def test_conventional_recipe_lines_for_creamy_tomato_ingredients() -> None:
    oil = ExtractedIngredient(
        id="ing_oil",
        name="olive oil",
        source_text="Olive oil",
    )
    tomatoes = ExtractedIngredient(
        id="ing_tomatoes",
        name="crushed tomatoes",
        quantity="1",
        unit="cup",
        source_text="1 cup crushed tomatoes",
    )
    cream = ExtractedIngredient(
        id="ing_cream",
        name="heavy cream",
        quantity="1/2",
        unit="cup",
        source_text="1/2 cup heavy cream",
    )
    salt = ExtractedIngredient(
        id="ing_salt",
        name="salt",
        notes="to taste",
        source_text="Salt to taste",
    )
    garlic = ExtractedIngredient(
        id="ing_garlic",
        name="garlic cloves",
        quantity="2",
        unit="cloves",
        notes="2 cloves minced",
        source_text="2 cloves minced garlic",
        list_status="instruction_only",
    )
    parmesan = ExtractedIngredient(
        id="ing_parm",
        name="Parmesan",
        notes="grated",
        source_text="grated Parmesan",
        evidence=SourceEvidence(quote="finish with grated Parmesan"),
    )

    assert format_ingredient_line(oil) == "Olive oil"
    assert format_ingredient_line(tomatoes) == "1 cup crushed tomatoes"
    assert format_ingredient_line(cream) == "1/2 cup heavy cream"
    assert format_ingredient_line(salt) == "Salt — to taste"
    assert format_ingredient_line(garlic) == "2 cloves garlic, minced"
    assert format_ingredient_line(parmesan) == "Parmesan, grated"
    assert parmesan.evidence is not None
    assert parmesan.evidence.quote == "finish with grated Parmesan"
    assert garlic.source_text == "2 cloves minced garlic"
    assert garlic.notes == "2 cloves minced"
    assert salt.quantity is None


def test_to_taste_is_not_a_missing_quantity_finding() -> None:
    result = ExtractionResult(
        title="Seasoning",
        ingredients=[
            ExtractedIngredient(
                id="ing_oil",
                name="olive oil",
                list_status="listed",
                source_text="Olive oil",
            ),
            ExtractedIngredient(
                id="ing_salt",
                name="salt",
                notes="to taste",
                list_status="listed",
                source_text="Salt to taste",
            ),
        ],
        steps=[ExtractedStep(id="step_1", text="Season.")],
    )
    normalized = normalize_result(result, [])
    missing = [
        finding
        for finding in normalized.findings
        if finding.type == "missing_quantity"
    ]
    assert any("ing_oil" in finding.related_ids for finding in missing)
    assert not any("ing_salt" in finding.related_ids for finding in missing)
    salt = next(item for item in normalized.ingredients if item.id == "ing_salt")
    assert salt.quantity is None
    assert format_ingredient_line(salt) == "Salt — to taste"
