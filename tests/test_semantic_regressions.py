from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from recipe_cooking_assistant.app import create_app
from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.extraction import StaticExtractionClient, normalize_result
from recipe_cooking_assistant.ingredient_display import (
    format_ingredient_line,
    group_listed_ingredients,
)
from recipe_cooking_assistant.models import (
    ExtractedIngredient,
    ExtractedStep,
    ExtractionResult,
    SourceEvidence,
)
from recipe_cooking_assistant.normalize import normalize_servings


def test_normalize_servings_strips_word() -> None:
    assert normalize_servings("4 servings") == "4"
    assert normalize_servings("Serves 4") == "4"
    assert normalize_servings("4") == "4"


def test_instruction_only_not_in_listed_and_creates_finding() -> None:
    result = ExtractionResult(
        title="Creamy Tomato Pasta",
        ingredients=[
            ExtractedIngredient(
                id="ing_oil",
                name="olive oil",
                quantity=None,
                source_text="Olive oil",
                list_status="listed",
            ),
            ExtractedIngredient(
                id="ing_garlic",
                name="garlic",
                quantity=None,
                list_status="instruction_only",
                evidence=SourceEvidence(quote="Add 2 cloves minced garlic"),
            ),
            ExtractedIngredient(
                id="ing_parm",
                name="Parmesan",
                quantity="1/4 cup",  # invented by a bad model
                list_status="instruction_only",
                evidence=SourceEvidence(quote="finish with grated Parmesan"),
            ),
        ],
        steps=[ExtractedStep(id="step_1", text="Cook.")],
    )
    normalized = normalize_result(result, [])
    listed_names = [i.name.lower() for i in normalized.listed_ingredients()]
    assert "garlic" not in listed_names
    assert "parmesan" not in listed_names
    parm = next(i for i in normalized.instruction_only_ingredients() if "parmesan" in i.name.lower())
    assert parm.quantity is None
    assert any(i.name.lower() == "garlic" for i in normalized.instruction_only_ingredients())
    assert any(
        f.type == "instruction_only_ingredient" and "ing_garlic" in f.related_ids
        for f in normalized.findings
    )
    assert "Referenced in instructions but absent from ingredient list" in " ".join(
        f.message for f in normalized.findings
    )


def test_alternatives_grouped_not_both_required_display() -> None:
    turkey = ExtractedIngredient(
        id="ing_1",
        name="ground turkey",
        quantity="1",
        unit="pound",
        alternative_group_id="alt_1",
        source_text="1 pound ground turkey",
    )
    soy = ExtractedIngredient(
        id="ing_2",
        name="soy crumbles",
        quantity="12",
        unit="ounces",
        alternative_group_id="alt_1",
        source_text="12 ounces soy crumbles",
    )
    groups = group_listed_ingredients([turkey, soy])
    assert len(groups) == 1
    gid, members = groups[0]
    assert gid == "alt_1"
    assert len(members) == 2


def test_package_source_text_preferred_in_display() -> None:
    item = ExtractedIngredient(
        id="ing_1",
        name="whole peeled tomatoes",
        quantity="14.5",
        unit="ounces",
        package_count="One",
        package_size="14 1/2-ounce",
        package_type="can",
        notes="with their juice",
        source_text="One 14 1/2-ounce can whole peeled tomatoes, with their juice",
    )
    line = format_ingredient_line(item)
    assert "14 1/2-ounce can" in line
    assert "with their juice" in line


def test_recipe_page_shows_findings_not_instruction_only_as_source(
    tmp_path: Path,
) -> None:
    result = ExtractionResult(
        title="Creamy Tomato Pasta",
        servings="4 servings",
        ingredients=[
            ExtractedIngredient(
                id="ing_1",
                name="penne",
                quantity="8",
                unit="ounces",
                source_text="8 ounces penne",
            ),
            ExtractedIngredient(
                id="ing_garlic",
                name="garlic",
                list_status="instruction_only",
                evidence=SourceEvidence(quote="Add 2 cloves minced garlic"),
            ),
        ],
        steps=[ExtractedStep(id="step_1", text="Cook.")],
    )
    app = create_app(
        Settings(session_secret="t", data_dir=tmp_path),
        extraction_client=StaticExtractionClient(result),
    )
    with TestClient(app) as client:
        response = client.post(
            "/import",
            data={"recipe_text": "pasta recipe"},
            follow_redirects=True,
        )
    assert response.status_code == 200
    assert "Servings: 4" in response.text
    assert "Servings: 4 servings" not in response.text
    assert "Instruction only" in response.text
    assert "Referenced in instructions but absent from ingredient list" in response.text
    # Listed section should still show penne as Source
    assert "8 ounces penne" in response.text
