from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from recipe_cooking_assistant.app import create_app
from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.db import isoformat, utcnow
from recipe_cooking_assistant.extraction import StaticExtractionClient
from recipe_cooking_assistant.models import (
    ExtractedIngredient,
    ExtractedStep,
    ExtractionFinding,
    ExtractionResult,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        session_secret="test-secret",
        data_dir=tmp_path,
        session_ttl_hours=24,
        max_images=6,
        max_upload_bytes=4 * 1024 * 1024,
        openai_api_key=None,
        openai_model="gpt-4.1-mini",
    )


def _client(tmp_path: Path, result: ExtractionResult) -> TestClient:
    app = create_app(
        _settings(tmp_path),
        extraction_client=StaticExtractionClient(result),
    )
    return TestClient(app)


def _import(client: TestClient) -> str:
    response = client.post("/import", data={"recipe_text": "recipe"}, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"]


def _pasta() -> ExtractionResult:
    return ExtractionResult(
        title="Garlic pasta",
        servings="3",
        ingredients=[
            ExtractedIngredient(
                id="ing_garlic",
                name="garlic",
                quantity="3",
                unit="cloves",
                notes="minced",
                list_status="listed",
                evidence=None,
            ),
            ExtractedIngredient(
                id="ing_salt",
                name="salt",
                notes="to taste",
                list_status="listed",
            ),
        ],
        steps=[
            ExtractedStep(id="step_1", text="Boil the pasta."),
            ExtractedStep(id="step_2", text="Cook the garlic for 30 seconds."),
        ],
    )


def test_one_ingredient_edit_hides_the_rest_of_the_recipe(tmp_path: Path) -> None:
    with _client(tmp_path, _pasta()) as client:
        location = _import(client)
        page = client.get(f"{location}/edit?ingredient=ing_garlic")
        assert page.status_code == 200
        assert "3 cloves garlic, minced" in page.text
        assert "Save ingredient" in page.text
        assert "Boil the pasta." not in page.text
        assert "Salt" not in page.text
        saved = client.post(
            f"{location}/edit",
            data={
                "action": "save_ingredient",
                "ingredient_id": "ing_garlic",
                "focus_ingredient": "ing_garlic",
                "name": "shallot",
                "quantity": "3",
                "unit": "cloves",
                "notes": "minced",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert saved.headers["location"].endswith("?ingredient=ing_garlic")
        replaced = client.post(
            f"{location}/edit",
            data={
                "action": "save_ingredient",
                "ingredient_id": "ing_garlic",
                "focus_ingredient": "ing_garlic",
                "name": "garlic powder",
                "quantity": "",
                "unit": "",
                "notes": "",
                "return_to": "prepare",
            },
            follow_redirects=False,
        )
        assert replaced.status_code == 303
        assert replaced.headers["location"].endswith("/prepare")
        prepare = client.get(replaced.headers["location"])
        assert "Garlic powder" in prepare.text
        assert "cloves" not in replaced.text
        assert "minced" not in replaced.text


def test_title_and_servings_do_not_scale(tmp_path: Path) -> None:
    with _client(tmp_path, _pasta()) as client:
        location = _import(client)
        page = client.get(f"{location}/edit")
        assert page.status_code == 200
        assert "does not scale" in page.text
        saved = client.post(
            f"{location}/edit",
            data={"action": "save_overview", "title": "Weeknight pasta", "servings": "4"},
            follow_redirects=True,
        )
        assert saved.status_code == 200
        assert "Weeknight pasta" in saved.text
        assert 'value="4"' in saved.text
        recipe = client.get(location)
        assert "Weeknight pasta" in recipe.text
        assert "Servings: 4" in recipe.text
        assert "3 cloves" in recipe.text.lower() or "3 cloves garlic" in recipe.text.lower()
        payload = json.loads(_payload(client))
        assert payload["title"] == "Garlic pasta"
        assert payload["servings"] == "3"
        assert payload["ingredients"][0]["quantity"] == "3"


def test_ingredient_field_provenance_and_add_remove(tmp_path: Path) -> None:
    with _client(tmp_path, _pasta()) as client:
        location = _import(client)
        edited = client.post(
            f"{location}/edit",
            data={
                "action": "save_ingredient",
                "ingredient_id": "ing_garlic",
                "name": "garlic",
                "quantity": "4",
                "unit": "cloves",
                "notes": "minced",
            },
            follow_redirects=True,
        )
        assert edited.status_code == 200
        from recipe_cooking_assistant.routes.recipe_routes import prepare_recipe

        prepared = prepare_recipe(
            client.app.state.db, location.rstrip("/").split("/")[-1], _session(client)
        )
        assert prepared is not None
        garlic = next(item for item in prepared.working.ingredients if item.id == "ing_garlic")
        assert garlic.quantity == "4"
        assert garlic.quantity_provenance == "user_edit"
        assert garlic.name_provenance is None
        assert garlic.unit_provenance is None
        assert garlic.notes_provenance is None
        added = client.post(
            f"{location}/edit",
            data={"action": "add_ingredient", "name": "chili flakes", "quantity": "1", "unit": "pinch"},
            follow_redirects=True,
        )
        assert "chili flakes" in added.text.lower() or "Chili flakes" in added.text
        removed = client.post(
            f"{location}/edit",
            data={"action": "remove_ingredient", "ingredient_id": "ing_salt"},
            follow_redirects=True,
        )
        assert "Restore ingredient" in removed.text
        prepared = prepare_recipe(
            client.app.state.db, location.rstrip("/").split("/")[-1], _session(client)
        )
        names = [item.name for item in prepared.working.listed_ingredients()]
        assert "salt" not in names
        assert any(item.id == "user_ing_1" for item in prepared.working.ingredients)
        payload = json.loads(_payload(client))
        assert any(item["id"] == "ing_salt" for item in payload["ingredients"])
        decisions = client.app.state.db.list_review_decisions(
            prepared.stored.id, _session(client)
        )
        assert decisions == []


def test_steps_edit_add_remove_reorder(tmp_path: Path) -> None:
    with _client(tmp_path, _pasta()) as client:
        location = _import(client)
        client.post(
            f"{location}/edit",
            data={"action": "save_step", "step_id": "step_1", "text": "Boil the pasta until tender."},
            follow_redirects=True,
        )
        client.post(
            f"{location}/edit",
            data={"action": "add_step", "text": "Finish with salt."},
            follow_redirects=True,
        )
        client.post(
            f"{location}/edit",
            data={"action": "move_step", "step_id": "user_step_1", "direction": "up"},
            follow_redirects=True,
        )
        client.post(
            f"{location}/edit",
            data={"action": "remove_step", "step_id": "step_2"},
            follow_redirects=True,
        )
        from recipe_cooking_assistant.routes.recipe_routes import prepare_recipe

        prepared = prepare_recipe(
            client.app.state.db, location.rstrip("/").split("/")[-1], _session(client)
        )
        texts = [step.text for step in prepared.working.steps]
        assert texts[0] == "Boil the pasta until tender."
        assert "Finish with salt." in texts
        assert "Cook the garlic for 30 seconds." not in texts
        assert any(step.id == "step_2" for step in prepared.reviewed.steps)


def test_invalid_edit_does_not_save_and_cancel_is_a_link(tmp_path: Path) -> None:
    with _client(tmp_path, _pasta()) as client:
        location = _import(client)
        bad = client.post(
            f"{location}/edit",
            data={"action": "save_ingredient", "ingredient_id": "ing_garlic", "name": "  "},
        )
        assert bad.status_code == 400
        assert "Enter name." in bad.text
        assert 'href="' + location + '"' in bad.text or "Cancel" in bad.text
        from recipe_cooking_assistant.routes.recipe_routes import prepare_recipe

        prepared = prepare_recipe(
            client.app.state.db, location.rstrip("/").split("/")[-1], _session(client)
        )
        garlic = next(item for item in prepared.working.ingredients if item.id == "ing_garlic")
        assert garlic.quantity == "3"


def test_pending_review_blocks_direct_edits(tmp_path: Path) -> None:
    result = _pasta()
    result.findings = [
        ExtractionFinding(
            id="find_1",
            type="missing_quantity",
            message="Salt has no amount.",
            related_ids=["ing_salt"],
        )
    ]
    with _client(tmp_path, result) as client:
        location = _import(client)
        page = client.get(location)
        assert "Edit recipe" not in page.text
        assert "Finish review" in page.text
        blocked = client.post(
            f"{location}/edit",
            data={"action": "save_overview", "title": "Nope", "servings": "9"},
        )
        assert blocked.status_code == 400
        assert "Finish every review item" in blocked.text


def test_same_working_recipe_on_prepare_cook_and_revert(tmp_path: Path) -> None:
    with _client(tmp_path, _pasta()) as client:
        location = _import(client)
        client.post(
            f"{location}/edit",
            data={"action": "save_overview", "title": "Edited pasta", "servings": "3"},
            follow_redirects=True,
        )
        recipe = client.get(location)
        prepare = client.get(f"{location}/prepare")
        cook = client.get(f"{location}/cook", follow_redirects=True)
        assert "Edited pasta" in recipe.text
        assert "Edited pasta" in prepare.text
        assert "Edited pasta" in cook.text
        client.post(f"{location}/edit", data={"action": "revert_overview"}, follow_redirects=True)
        restored = client.get(location)
        assert "Garlic pasta" in restored.text
        assert "Edited pasta" not in restored.text


def test_other_session_and_expiry_drop_edits(tmp_path: Path) -> None:
    with _client(tmp_path, _pasta()) as client:
        location = _import(client)
        recipe_id = location.rstrip("/").split("/")[-1]
        client.post(
            f"{location}/edit",
            data={"action": "add_ingredient", "name": "butter"},
            follow_redirects=True,
        )
        other = TestClient(client.app)
        assert other.get(f"{location}/edit").status_code == 404
        past = isoformat(utcnow() - timedelta(hours=2))
        db = client.app.state.db
        with db.connect() as conn:
            conn.execute(
                "UPDATE extracted_recipes SET expires_at = ? WHERE id = ?",
                (past, recipe_id),
            )
        assert client.get(f"{location}/edit").status_code == 404
        db.purge_expired()
        with db.connect() as conn:
            edits = conn.execute("SELECT COUNT(*) AS n FROM recipe_edits").fetchone()["n"]
        assert edits == 0


def _payload(client: TestClient) -> str:
    with client.app.state.db.connect() as conn:
        row = conn.execute("SELECT payload_json FROM extracted_recipes").fetchone()
    assert row is not None
    return row["payload_json"]


def _session(client: TestClient) -> str:
    with client.app.state.db.connect() as conn:
        row = conn.execute("SELECT session_id FROM extracted_recipes").fetchone()
    assert row is not None
    return row["session_id"]
