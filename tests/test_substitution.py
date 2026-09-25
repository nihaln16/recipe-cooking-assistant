from __future__ import annotations

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
class RecordingGuidance:
    def __init__(self, replies: list[str] | None = None):
        self.replies = replies or ["Try a different ingredient. This may change texture."]
        self.calls: list[dict] = []

    def advise(self, *, context, settings):
        self.calls.append(context)
        return self.replies[(len(self.calls) - 1) % len(self.replies)]


def _client(tmp_path: Path, result: ExtractionResult, guidance: RecordingGuidance, **overrides):
    app = create_app(
        Settings(
            session_secret="test-secret",
            data_dir=tmp_path,
            session_ttl_hours=24,
            max_images=6,
            max_upload_bytes=4 * 1024 * 1024,
            openai_api_key=None,
            openai_model="gpt-4.1-mini",
            **overrides,
        ),
        extraction_client=StaticExtractionClient(result),
        guidance_client=guidance,
    )
    return TestClient(app)


def _recipe() -> ExtractionResult:
    return ExtractionResult(
        title="Tacos",
        servings="2",
        ingredients=[
            ExtractedIngredient(
                id="ing_beef",
                name="beef",
                quantity="8",
                unit="ounces",
                notes="ground",
                list_status="listed",
                alternative_group_id="protein",
            ),
            ExtractedIngredient(
                id="ing_lentils",
                name="lentils",
                quantity="1",
                unit="cup",
                list_status="listed",
                alternative_group_id="protein",
            ),
            ExtractedIngredient(
                id="ing_salt",
                name="salt",
                notes="to taste",
                list_status="listed",
            ),
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Brown the beef.",
                related_ingredient_ids=["ing_beef"],
            )
        ],
    )


def _import(client: TestClient) -> str:
    response = client.post("/import", data={"recipe_text": "tacos"}, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"]


def _token(html: str) -> str:
    marker = 'name="form_token" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def test_source_alternatives_need_no_model_call(tmp_path: Path) -> None:
    guide = RecordingGuidance()
    with _client(tmp_path, _recipe(), guide) as client:
        location = _import(client)
        page = client.get(f"{location}/substitute/ing_beef")
        assert page.status_code == 200
        assert "Source alternatives" in page.text
        assert "lentils" in page.text.lower()
        assert "AI guidance" not in page.text or "Replies are AI guidance" in page.text
        assert guide.calls == []


def test_substitution_is_grounded_isolated_and_does_not_mutate(tmp_path: Path) -> None:
    guide = RecordingGuidance(replies=["Try lentils. This may change texture and is not an allergen guarantee."])
    with _client(tmp_path, _recipe(), guide) as client:
        location = _import(client)
        original = client.get(location).text
        page = client.get(f"{location}/substitute/ing_beef")
        sent = client.post(
            f"{location}/substitute/ing_beef",
            data={"quick_action": "suggest", "form_token": _token(page.text)},
            follow_redirects=True,
        )
        assert "AI guidance" in sent.text
        assert guide.calls
        context = guide.calls[0]
        assert context["_purpose"] == "substitution"
        assert context["target_ingredient"]["name"] == "beef"
        assert context["target_ingredient"]["quantity"] == "8"
        assert context["target_ingredient"]["notes"] == "ground"
        assert context["servings"] == "2"
        assert any(item["name"] == "lentils" for item in context["source_alternatives"])
        assert context["linked_steps"][0]["text"] == "Brown the beef."
        follow = client.get(f"{location}/substitute/ing_beef")
        client.post(
            f"{location}/substitute/ing_beef",
            data={"message": "Will that change the cooking time?", "form_token": _token(follow.text)},
            follow_redirects=True,
        )
        assert guide.calls[1]["recent_messages"][-1]["text"].startswith("Will that")
        salt = client.get(f"{location}/substitute/ing_salt")
        assert "Source alternatives" not in salt.text
        other = TestClient(client.app)
        assert other.get(f"{location}/substitute/ing_beef").status_code == 404
        removed = client.post(
            f"{location}/edit",
            data={"action": "remove_ingredient", "ingredient_id": "ing_salt"},
            follow_redirects=True,
        )
        assert removed.status_code == 200
        gone = client.get(f"{location}/substitute/ing_salt")
        assert gone.status_code == 400
        assert "Edited" not in original
        recipe = client.get(location)
        assert "8 ounces" in recipe.text.lower() or "8 ounces beef" in recipe.text.lower()


def test_invalid_and_rate_limited_substitution_skips_the_model(tmp_path: Path) -> None:
    guide = RecordingGuidance()
    with _client(tmp_path, _recipe(), guide, guidance_limit_per_session=1) as client:
        location = _import(client)
        page = client.get(f"{location}/substitute/ing_beef")
        empty = client.post(
            f"{location}/substitute/ing_beef",
            data={"message": "  ", "form_token": _token(page.text)},
        )
        assert empty.status_code == 400
        huge_page = client.get(f"{location}/substitute/ing_beef")
        huge = client.post(
            f"{location}/substitute/ing_beef",
            data={"message": "x" * 900, "form_token": _token(huge_page.text)},
        )
        assert huge.status_code == 400
        assert guide.calls == []
        ok_page = client.get(f"{location}/substitute/ing_beef")
        client.post(
            f"{location}/substitute/ing_beef",
            data={"quick_action": "suggest", "form_token": _token(ok_page.text)},
            follow_redirects=True,
        )
        assert len(guide.calls) == 1
        limited_page = client.get(f"{location}/substitute/ing_beef")
        limited = client.post(
            f"{location}/substitute/ing_beef",
            data={"message": "What about texture?", "form_token": _token(limited_page.text)},
        )
        assert limited.status_code == 429
        assert len(guide.calls) == 1


def test_manual_replacement_is_a_user_edit(tmp_path: Path) -> None:
    guide = RecordingGuidance()
    with _client(tmp_path, _recipe(), guide) as client:
        location = _import(client)
        page = client.get(f"{location}/substitute/ing_beef")
        assert "Edit ingredient" in page.text
        client.post(
            f"{location}/edit",
            data={
                "action": "save_ingredient",
                "ingredient_id": "ing_beef",
                "name": "mushrooms",
                "quantity": "8",
                "unit": "ounces",
                "notes": "ground",
            },
            follow_redirects=True,
        )
        from recipe_cooking_assistant.routes.recipe_routes import prepare_recipe

        with client.app.state.db.connect() as conn:
            session_id = conn.execute("SELECT session_id FROM extracted_recipes").fetchone()["session_id"]
        recipe_id = location.rstrip("/").split("/")[-1]
        prepared = prepare_recipe(client.app.state.db, recipe_id, session_id)
        beef = next(item for item in prepared.working.ingredients if item.id == "ing_beef")
        assert beef.name == "mushrooms"
        assert beef.name_provenance == "user_edit"
        assert beef.quantity_provenance is None
        assert guide.calls == []
