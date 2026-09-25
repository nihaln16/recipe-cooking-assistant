from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from recipe_cooking_assistant.app import create_app
from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.extraction import StaticExtractionClient
from recipe_cooking_assistant.models import (
    ExtractedIngredient,
    ExtractedStep,
    ExtractionResult,
)
from recipe_cooking_assistant.scaling import build_scaled_view, parse_serving_count


class ScalingGuidance:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def advise_scaling(self, *, context, settings):
        self.calls.append(context)
        return "Keep the same temperature. A larger batch may take longer to become fragrant."


def _recipe() -> ExtractionResult:
    return ExtractionResult(
        title="Skillet",
        servings="4",
        ingredients=[
            ExtractedIngredient(id="ing_pasta", name="spaghetti", quantity="8", unit="ounces", list_status="listed"),
            ExtractedIngredient(id="ing_garlic", name="garlic", quantity="3", unit="cloves", list_status="listed"),
            ExtractedIngredient(id="ing_milk", name="milk", quantity="1/2", unit="cup", list_status="listed"),
            ExtractedIngredient(id="ing_beans", name="beans", package_count="2", package_size="15 oz", package_type="cans", list_status="listed"),
            ExtractedIngredient(id="ing_beef", name="beef", quantity="1", unit="pound", alternative_group_id="protein", list_status="listed"),
            ExtractedIngredient(id="ing_lentils", name="lentils", quantity="1", unit="cup", alternative_group_id="protein", list_status="listed"),
            ExtractedIngredient(id="ing_cilantro", name="cilantro", optional=True, quantity="2", unit="tablespoons", list_status="listed"),
            ExtractedIngredient(id="ing_salt", name="salt", notes="to taste", list_status="listed"),
            ExtractedIngredient(id="ing_oil", name="olive oil", list_status="listed"),
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Cook the garlic for 30 seconds at 350°F in a 10-inch skillet.",
                related_ingredient_ids=["ing_garlic"],
            )
        ],
    )


def _client(tmp_path: Path, guidance: ScalingGuidance | None = None) -> TestClient:
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
        extraction_client=StaticExtractionClient(_recipe()),
        guidance_client=guidance,
    )
    return TestClient(app)


def _import(client: TestClient) -> str:
    response = client.post("/import", data={"recipe_text": "skillet"}, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"]


def _clear_review(client: TestClient, location: str) -> None:
    for finding_id in ("find_missing_qty_ing_oil", "find_missing_qty_ing_beans"):
        posted = client.post(
            f"{location}/review/{finding_id}",
            data={"action": "accept"},
            follow_redirects=False,
        )
        assert posted.status_code == 303


def _payload(client: TestClient) -> str:
    with client.app.state.db.connect() as conn:
        row = conn.execute("SELECT payload_json FROM extracted_recipes").fetchone()
    return row["payload_json"]


def test_double_scales_quantities_without_changing_the_recipe(tmp_path: Path) -> None:
    assert parse_serving_count("4 to 6") is None
    with _client(tmp_path) as client:
        location = _import(client)
        original = _payload(client)
        _clear_review(client, location)
        blocked = client.post(f"{location}/scale", data={"action": "set", "target": "4"})
        assert blocked.status_code == 400
        doubled = client.post(f"{location}/scale", data={"action": "double"}, follow_redirects=True)
        assert doubled.status_code == 200
        assert "For 8 servings" in doubled.text
        assert "16 ounces" in doubled.text
        assert "6 cloves" in doubled.text
        assert "1 cup" in doubled.text
        assert "4 cans" in doubled.text
        assert "15 oz" in doubled.text
        assert "to taste" in doubled.text
        assert "Not scaled" in doubled.text
        assert "Quantity left blank" in doubled.text
        assert "Calculated" in doubled.text
        assert "Source step" not in doubled.text
        assert "Confirm quantities" in doubled.text
        confirmed = client.post(f"{location}/scale/confirm", follow_redirects=True)
        assert "Prepare" in confirmed.text
        assert "Servings: 8" in confirmed.text
        assert "16 ounces" in confirmed.text
        cooking = client.get(f"{location}/cook/1")
        assert "6 cloves" in cooking.text
        assert "350°F" in cooking.text
        assert "30 seconds" in cooking.text
        assert "10-inch" in cooking.text
        recipe = client.get(location)
        assert "Servings: 4" in recipe.text
        assert "8 ounces" in recipe.text
        assert _payload(client) == original


def test_guidance_confirms_without_mutating_and_rolls_back(tmp_path: Path) -> None:
    guide = ScalingGuidance()
    with _client(tmp_path, guide) as client:
        location = _import(client)
        original = _payload(client)
        _clear_review(client, location)
        early = client.post(f"{location}/scale/guide")
        assert early.status_code == 400
        assert guide.calls == []
        client.post(f"{location}/scale", data={"action": "double"}, follow_redirects=True)
        guided = client.post(f"{location}/scale/guide", follow_redirects=True)
        assert "Keep the same temperature" in guided.text
        assert len(guide.calls) == 1
        assert guide.calls[0]["target_servings"] == "8"
        assert any(row["skipped_reason"] == "missing" for row in guide.calls[0]["calculated_ingredients"])
        cached = client.post(f"{location}/scale/guide", follow_redirects=True)
        assert cached.status_code == 200
        assert len(guide.calls) == 1
        assert guided.text.count("Confirm quantities") == 1
        assert "id=\"scale-guidance\"" in guided.text
        confirmed = client.post(
            f"{location}/scale/confirm",
            data={"suggestion": "Keep the same temperature. Check earlier."},
            follow_redirects=False,
        )
        assert confirmed.status_code == 303
        assert confirmed.headers["location"].endswith("/prepare")
        prepared = client.get(confirmed.headers["location"])
        assert "User edit" in prepared.text
        assert "Check earlier" in prepared.text
        assert "16 ounces" in prepared.text
        recipe = client.get(location)
        assert "Servings: 4" in recipe.text
        assert _payload(client) == original
        client.post(f"{location}/scale/rollback", follow_redirects=True)
        cleared = client.get(f"{location}/scale")
        assert "For 8 servings" not in cleared.text
        other = TestClient(client.app)
        assert other.get(f"{location}/scale").status_code == 404


def test_range_servings_are_not_scaled(tmp_path: Path) -> None:
    result = _recipe()
    result.servings = "4 to 6"
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
    with TestClient(app) as client:
        location = _import(client)
        _clear_review(client, location)
        response = client.post(f"{location}/scale", data={"action": "double"})
        assert response.status_code == 400
        assert "single count" in response.text
        view = build_scaled_view(_recipe(), "8")
        garlic = next(row for row in view["ingredients"] if row["id"] == "ing_garlic")
        assert garlic["quantity"] == "6"
        assert json.loads(_payload(client))["servings"] == "4 to 6"
