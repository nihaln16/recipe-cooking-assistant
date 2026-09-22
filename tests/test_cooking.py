from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from recipe_cooking_assistant.app import create_app
from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.cooking import (
    CookCursor,
    cursor_for,
    ingredients_for_step,
    resume_index,
    store_cursor,
)
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


def _import(client: TestClient, text: str = "recipe") -> str:
    response = client.post(
        "/import",
        data={"recipe_text": text},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"]


def _recipe_id(location: str) -> str:
    return location.rstrip("/").split("/")[-1]


def _payload_json(client: TestClient) -> str:
    db = client.app.state.db
    with db.connect() as conn:
        row = conn.execute("SELECT payload_json FROM extracted_recipes").fetchone()
    assert row is not None
    return row["payload_json"]


def _back_button(html: str) -> str:
    match = re.search(
        r'<button class="btn btn-secondary"[^>]*>Back</button>', html
    )
    assert match is not None
    return match.group(0)


def _steps(
    texts: list[str], related: list[list[str]] | None = None
) -> list[ExtractedStep]:
    related = related or [[] for _ in texts]
    return [
        ExtractedStep(
            id=f"step_{index}",
            text=text,
            related_ingredient_ids=ids,
            provenance="source",
        )
        for index, (text, ids) in enumerate(zip(texts, related, strict=True), start=1)
    ]


def _listed(
    ingredient_id: str, name: str, quantity: str = "1", unit: str = "cup"
) -> ExtractedIngredient:
    return ExtractedIngredient(
        id=ingredient_id,
        name=name,
        quantity=quantity,
        unit=unit,
        list_status="listed",
        provenance="source",
    )


def test_cursor_round_trip_keeps_recipes_separate() -> None:
    session: dict = {}
    store_cursor(session, "recipe-a", CookCursor(index=2, done=False))
    store_cursor(session, "recipe-b", CookCursor(index=0, done=True))
    assert cursor_for(session, "recipe-a") == CookCursor(index=2, done=False)
    assert cursor_for(session, "recipe-b") == CookCursor(index=0, done=True)
    tainted = {"cook_progress": {"recipe-a": {"index": True, "done": "yes"}}}
    assert cursor_for(tainted, "recipe-a") == CookCursor()
    assert resume_index(CookCursor(index=1, done=False), 3) == 1
    assert resume_index(CookCursor(index=8, done=False), 3) == 0


def test_ingredients_for_step_require_listed_ids() -> None:
    working = ExtractionResult(
        ingredients=[
            _listed("ing_chicken", "chicken breast", "1", "pound"),
            _listed("ing_tofu", "firm tofu", "14", "ounces"),
            _listed("ing_nutmeg", "nutmeg", "1", "teaspoon"),
            ExtractedIngredient(
                id="ing_asa",
                name="asafoetida",
                list_status="instruction_only",
                provenance="source",
            ),
        ],
        steps=_steps(["Bloom the saffron, then sear."]),
    )
    working.ingredients[0].alternative_group_id = "alt_protein"
    working.ingredients[1].alternative_group_id = "alt_protein"
    groups = ingredients_for_step(
        working, ["ing_chicken", "ing_asa", "ing_missing"]
    )
    assert [member.id for _, members in groups for member in members] == [
        "ing_chicken",
        "ing_tofu",
    ]


def test_start_cooking_when_nothing_needs_review(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Toast",
        ingredients=[_listed("ing_bread", "bread", "2", "slices")],
        steps=_steps(["Toast the bread."], [["ing_bread"]]),
    )
    with _client(tmp_path, result) as client:
        location = _import(client)
        page = client.get(location)
        assert page.status_code == 200
        assert f'href="{location}/cook"' in page.text
        assert "Review complete" not in page.text


def test_unresolved_review_blocks_cooking_without_deciding(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Oil",
        ingredients=[
            ExtractedIngredient(
                id="ing_oil",
                name="olive oil",
                list_status="listed",
                provenance="source",
            )
        ],
        steps=_steps(["Heat the pan."]),
    )
    with _client(tmp_path, result) as client:
        location = _import(client)
        recipe_id = _recipe_id(location)
        page = client.get(location)
        assert f'href="{location}/cook"' not in page.text
        blocked = client.get(f"{location}/cook")
        assert blocked.status_code == 400
        assert "Decide every review item before cooking" in blocked.text
        assert f'href="{location}"' in blocked.text
        assert 'value="next"' not in blocked.text
        assert 'value="accept"' not in blocked.text
        posted = client.post(
            f"{location}/cook", data={"action": "next", "step": "1"}
        )
        assert posted.status_code == 400
        still = client.get(location)
        assert f'href="{location}/cook"' not in still.text
        db = client.app.state.db
        with db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM review_decisions WHERE extraction_id = ?",
                (recipe_id,),
            ).fetchone()["n"]
        assert count == 0


def test_navigation_edges_and_immutable_payload(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Onion pan",
        ingredients=[_listed("ing_onion", "onion")],
        steps=_steps(["Heat the pan.", "Add the onion.", "Serve warm."]),
    )
    with _client(tmp_path, result) as client:
        location = _import(client)
        original = _payload_json(client)

        first = client.get(f"{location}/cook", follow_redirects=True)
        assert first.status_code == 200
        assert "Step 1 of 3" in first.text
        assert "Heat the pan." in first.text
        assert "disabled" in _back_button(first.text)
        assert 'value="next"' in first.text
        assert "Finish" not in first.text

        stayed = client.post(
            f"{location}/cook",
            data={"action": "back", "step": "1"},
            follow_redirects=True,
        )
        assert "Step 1 of 3" in stayed.text

        second = client.post(
            f"{location}/cook",
            data={"action": "next", "step": "1"},
            follow_redirects=True,
        )
        assert "Step 2 of 3" in second.text
        assert "Add the onion." in second.text
        assert "disabled" not in _back_button(second.text)
        refreshed = client.get(f"{location}/cook/2")
        assert "Step 2 of 3" in refreshed.text
        assert "Add the onion." in refreshed.text

        for token in ("0", "foo", "2.5", "99"):
            invalid = client.get(f"{location}/cook/{token}")
            assert invalid.status_code == 400
            assert "That step is not part of this recipe." in invalid.text
            assert f'href="{location}"' in invalid.text
        posted_invalid = client.post(
            f"{location}/cook",
            data={"action": "next", "step": "99"},
        )
        assert posted_invalid.status_code == 400
        resume = client.get(f"{location}/cook", follow_redirects=False)
        assert resume.status_code == 303
        assert resume.headers["location"].endswith("/cook/2")

        last = client.post(
            f"{location}/cook",
            data={"action": "next", "step": "2"},
            follow_redirects=True,
        )
        assert "Step 3 of 3" in last.text
        assert "disabled" not in _back_button(last.text)
        assert 'value="finish"' in last.text
        assert 'value="next"' not in last.text

        done = client.post(
            f"{location}/cook",
            data={"action": "finish", "step": "3"},
            follow_redirects=True,
        )
        assert done.status_code == 200
        assert "Cooking complete" in done.text
        assert f'href="{location}"' in done.text
        again_blocked = client.get(f"{location}/cook/99")
        assert again_blocked.status_code == 400
        still_done = client.get(f"{location}/cook")
        assert "Cooking complete" in still_done.text

        restarted = client.post(
            f"{location}/cook",
            data={"action": "cook_again"},
            follow_redirects=True,
        )
        assert "Step 1 of 3" in restarted.text
        assert _payload_json(client) == original
        assert json.loads(original)["steps"][1]["text"] == "Add the onion."


def test_one_step_and_zero_steps(tmp_path: Path) -> None:
    one = ExtractionResult(
        title="Single",
        ingredients=[_listed("ing_bread", "bread", "2", "slices")],
        steps=_steps(["Toast the bread."]),
    )
    with _client(tmp_path, one) as client:
        location = _import(client)
        page = client.get(f"{location}/cook", follow_redirects=True)
        assert "Step 1 of 1" in page.text
        assert "disabled" in _back_button(page.text)
        assert 'value="finish"' in page.text
        assert 'value="next"' not in page.text
        missing = client.get(f"{location}/cook/2")
        assert missing.status_code == 400

    empty = ExtractionResult(
        title="Plain bread",
        ingredients=[_listed("ing_bread", "bread", "2", "slices")],
        steps=[],
    )
    with _client(tmp_path, empty) as client:
        location = _import(client)
        page = client.get(location)
        assert f'href="{location}/cook"' in page.text
        cooking = client.get(f"{location}/cook")
        assert cooking.status_code == 200
        assert "No steps to cook" in cooking.text
        assert f'href="{location}"' in cooking.text
        assert 'value="next"' not in cooking.text
        invalid = client.get(f"{location}/cook/1")
        assert invalid.status_code == 400
        assert "That step is not part of this recipe." in invalid.text


def test_working_recipe_edits_and_rejected_links(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Pan",
        ingredients=[
            ExtractedIngredient(
                id="ing_oil",
                name="olive oil",
                list_status="listed",
                provenance="source",
            ),
            ExtractedIngredient(
                id="ing_asa",
                name="asafoetida",
                list_status="instruction_only",
                provenance="source",
            ),
            _listed("ing_chicken", "chicken breast", "1", "pound"),
            _listed("ing_tofu", "firm tofu", "14", "ounces"),
            _listed("ing_saffron", "saffron", "1", "pinch"),
            _listed("ing_nutmeg", "nutmeg", "1", "teaspoon"),
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Bloom the saffron, then sear.",
                related_ingredient_ids=[
                    "ing_oil",
                    "ing_chicken",
                    "ing_asa",
                    "ing_missing",
                ],
                provenance="source",
            ),
            ExtractedStep(
                id="step_2",
                text="Simmer until done.",
                provenance="source",
            ),
        ],
        findings=[
            ExtractionFinding(
                id="find_step_2",
                type="uncertain_extraction",
                message="Check this step.",
                related_ids=["step_2"],
            )
        ],
    )
    result.ingredients[2].alternative_group_id = "alt_protein"
    result.ingredients[3].alternative_group_id = "alt_protein"

    with _client(tmp_path, result) as client:
        location = _import(client)
        original = _payload_json(client)
        edited = client.post(
            f"{location}/review/find_missing_qty_ing_oil",
            data={"action": "edit", "quantity": "3", "unit": "tablespoon"},
            follow_redirects=False,
        )
        assert edited.status_code == 303
        rejected = client.post(
            f"{location}/review/find_ing_asa",
            data={"action": "reject"},
            follow_redirects=False,
        )
        assert rejected.status_code == 303
        rewritten = client.post(
            f"{location}/review/find_step_2",
            data={"action": "edit", "text": "Fold for 30 seconds."},
            follow_redirects=False,
        )
        assert rewritten.status_code == 303
        page = client.get(f"{location}/cook", follow_redirects=True)
        assert page.status_code == 200
        assert "3 tablespoon olive oil" in page.text
        assert "User edit" in page.text
        assert "asafoetida" not in page.text.lower()
        assert "1 pound chicken breast" in page.text
        assert "14 ounces firm tofu" in page.text
        assert "Choose one" in page.text
        assert "1 pinch saffron" not in page.text
        assert "Bloom the saffron, then sear." in page.text
        assert "1 teaspoon nutmeg" not in page.text
        second = client.post(
            f"{location}/cook",
            data={"action": "next", "step": "1"},
            follow_redirects=True,
        )
        assert "Fold for 30 seconds." in second.text
        assert "Simmer until done." not in second.text
        assert "User edit" in second.text
        assert _payload_json(client) == original
        stored = json.loads(original)
        oil = next(item for item in stored["ingredients"] if item["id"] == "ing_oil")
        asa = next(item for item in stored["ingredients"] if item["id"] == "ing_asa")
        step = next(item for item in stored["steps"] if item["id"] == "step_2")
        assert oil["quantity"] is None
        assert asa["list_status"] == "instruction_only"
        assert step["text"] == "Simmer until done."


def test_expired_and_other_session_cannot_cook(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Toast",
        ingredients=[_listed("ing_bread", "bread", "2", "slices")],
        steps=_steps(["Toast the bread."]),
    )
    with _client(tmp_path, result) as client:
        location = _import(client)
        recipe_id = _recipe_id(location)
        client.get(f"{location}/cook", follow_redirects=True)
        other = TestClient(client.app)
        blocked = other.get(f"{location}/cook")
        assert blocked.status_code == 404
        assert "another session" in blocked.text
        other_step = other.get(f"{location}/cook/1")
        assert other_step.status_code == 404
        owner = client.get(f"{location}/cook", follow_redirects=False)
        assert owner.status_code == 303
        assert owner.headers["location"].endswith("/cook/1")

        past = isoformat(utcnow() - timedelta(hours=2))
        db = client.app.state.db
        with db.connect() as conn:
            conn.execute(
                "UPDATE extracted_recipes SET expires_at = ? WHERE id = ?",
                (past, recipe_id),
            )
        expired = client.get(f"{location}/cook")
        assert expired.status_code == 404
        assert "expired" in expired.text
        expired_step = client.get(f"{location}/cook/1")
        assert expired_step.status_code == 404
