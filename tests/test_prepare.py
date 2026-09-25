from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from recipe_cooking_assistant.app import create_app
from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.extraction import StaticExtractionClient
from recipe_cooking_assistant.models import (
    ExtractedIngredient,
    ExtractedStep,
    ExtractionFinding,
    ExtractionResult,
)


def _client(tmp_path: Path, result: ExtractionResult) -> TestClient:
    app = create_app(
        Settings(
            session_secret="test-secret",
            data_dir=tmp_path,
            session_ttl_hours=24,
            max_images=6,
            max_upload_bytes=4 * 1024 * 1024,
            openai_api_key=None,
            openai_model="gpt-4.1-mini",
        ),
        extraction_client=StaticExtractionClient(result),
    )
    return TestClient(app)


def _recipe() -> ExtractionResult:
    return ExtractionResult(
        title="Chili",
        servings="4",
        ingredients=[
            ExtractedIngredient(id="ing_beef", name="beef", quantity="1", unit="pound", list_status="listed", alternative_group_id="protein"),
            ExtractedIngredient(id="ing_turkey", name="turkey", quantity="1", unit="pound", list_status="listed", alternative_group_id="protein"),
            ExtractedIngredient(id="ing_onion", name="onion", quantity="1", list_status="listed"),
            ExtractedIngredient(id="ing_cilantro", name="cilantro", optional=True, notes="for garnish", list_status="listed"),
            ExtractedIngredient(id="ing_secret", name="secret spice", list_status="instruction_only"),
        ],
        steps=[ExtractedStep(id="step_1", text="Simmer the chili.")],
        findings=[
            ExtractionFinding(
                id="find_secret",
                type="instruction_only_ingredient",
                message="Secret spice is only in the steps.",
                related_ids=["ing_secret"],
            )
        ],
    )


def _import(client: TestClient) -> str:
    response = client.post("/import", data={"recipe_text": "chili"}, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"]


def test_prepare_requires_clear_review_and_lists_working_ingredients(tmp_path: Path) -> None:
    with _client(tmp_path, _recipe()) as client:
        location = _import(client)
        blocked = client.get(f"{location}/prepare")
        assert blocked.status_code == 400
        assert "Finish every review item" in blocked.text
        client.post(
            f"{location}/review/find_secret",
            data={"action": "reject"},
            follow_redirects=True,
        )
        page = client.get(f"{location}/prepare")
        assert page.status_code == 200
        assert "Choose one" in page.text
        assert "Optional" in page.text
        assert "secret spice" not in page.text.lower()
        assert "Start cooking" in page.text
        client.post(
            f"{location}/edit",
            data={"action": "add_ingredient", "name": "lime"},
            follow_redirects=True,
        )
        client.post(
            f"{location}/edit",
            data={"action": "remove_ingredient", "ingredient_id": "ing_onion"},
            follow_redirects=True,
        )
        updated = client.get(f"{location}/prepare")
        assert "lime" in updated.text.lower()
        assert "onion" not in updated.text.lower()
        assert "beef" in updated.text.lower()
    with _client(tmp_path, _recipe()) as client:
        location = _import(client)
        client.post(
            f"{location}/review/find_secret",
            data={"action": "accept"},
            follow_redirects=True,
        )
        accepted = client.get(f"{location}/prepare")
        assert "secret spice" in accepted.text.lower()


def test_checks_persist_do_not_block_or_change_recipe(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Chili",
        servings="4",
        ingredients=[
            ExtractedIngredient(id="ing_onion", name="onion", quantity="1", list_status="listed"),
        ],
        steps=[ExtractedStep(id="step_1", text="Simmer the chili.")],
    )
    with _client(tmp_path, result) as client:
        location = _import(client)
        before = client.get(location).text
        checked = client.post(
            f"{location}/prepare",
            data={"ingredient_id": "ing_onion", "checked": "1"},
            headers={"Accept": "application/json"},
        )
        assert checked.status_code == 200
        assert checked.json()["checked"] is True
        page = client.get(f"{location}/prepare")
        assert 'aria-pressed="true"' in page.text
        again = client.get(f"{location}/prepare")
        assert 'aria-pressed="true"' in again.text
        cook = client.get(f"{location}/cook", follow_redirects=True)
        assert cook.status_code == 200
        assert "Simmer the chili." in cook.text
        after = client.get(location).text
        assert "1 onion" in after.lower() or "Onion" in after
        other = TestClient(client.app)
        assert other.get(f"{location}/prepare").status_code == 404
        again_recipe = client.get(location)
        substitute = client.get(f"{location}/substitute/ing_onion")
        edit = client.get(f"{location}/edit")
        cook = client.get(f"{location}/cook", follow_redirects=True)
        returned = client.get(f"{location}/prepare")
        assert 'aria-pressed="true"' in returned.text
        assert returned.text.count("1 onion") == again.text.count("1 onion")
        assert "Mark ready" not in returned.text.split("check-box")[0]
        assert '<span class="check-box"' in returned.text
        assert "1 onion" in again_recipe.text
        assert "1 onion" in substitute.text
        assert "1 onion" in edit.text
        assert cook.status_code == 200
        second = client.get(f"{location}/prepare")
        assert second.text == returned.text
        assert before
