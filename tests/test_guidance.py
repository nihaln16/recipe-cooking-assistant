from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recipe_cooking_assistant.app import create_app
from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.db import isoformat, utcnow
from recipe_cooking_assistant.extraction import StaticExtractionClient
from recipe_cooking_assistant.guidance import (
    GUIDANCE_INSTRUCTIONS,
    MAX_CONTEXT_CHARS,
    MAX_HISTORY_MESSAGES,
    build_model_context,
    navigation_command,
    parse_guidance_output,
    quick_action_text,
    QUICK_ACTIONS,
)
from recipe_cooking_assistant.models import (
    ExtractedIngredient,
    ExtractedStep,
    ExtractionFinding,
    ExtractionResult,
    IgnoredBoilerplate,
)


class RecordingGuidance:
    def __init__(self, replies: list[str] | None = None, fail_after: int | None = None):
        self.replies = replies or ["The recipe does not specify. As a suggestion, use medium heat."]
        self.fail_after = fail_after
        self.calls: list[dict] = []

    def advise(self, *, context, settings):
        self.calls.append(context)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("provider exploded sk-testsecret /Users/nihal/secret")
        return self.replies[(len(self.calls) - 1) % len(self.replies)]


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        session_secret="test-secret",
        data_dir=tmp_path,
        session_ttl_hours=24,
        max_images=6,
        max_upload_bytes=4 * 1024 * 1024,
        openai_api_key=None,
        openai_model="gpt-4.1-mini",
    )
    values.update(overrides)
    return Settings(**values)


def _client(tmp_path: Path, result: ExtractionResult, guidance: RecordingGuidance, **overrides):
    app = create_app(
        _settings(tmp_path, **overrides),
        extraction_client=StaticExtractionClient(result),
        guidance_client=guidance,
    )
    return TestClient(app), guidance


def _import(client: TestClient) -> str:
    response = client.post("/import", data={"recipe_text": "recipe"}, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"]


def _token(html: str) -> str:
    match = re.search(r'name="form_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _payload(client: TestClient) -> str:
    with client.app.state.db.connect() as conn:
        row = conn.execute("SELECT payload_json FROM extracted_recipes").fetchone()
    return row["payload_json"]


def _recipe() -> ExtractionResult:
    return ExtractionResult(
        title="Garlic pasta",
        servings="2",
        ingredients=[
            ExtractedIngredient(
                id="ing_garlic",
                name="garlic",
                quantity="3",
                unit="cloves",
                list_status="listed",
                provenance="source",
            ),
            ExtractedIngredient(
                id="ing_cream",
                name="heavy cream",
                quantity="1",
                unit="cup",
                list_status="listed",
                provenance="source",
            ),
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Cook the garlic until fragrant.",
                related_ingredient_ids=["ing_garlic"],
                provenance="source",
            ),
            ExtractedStep(
                id="step_2",
                text="Stir in the cream.",
                related_ingredient_ids=["ing_cream"],
                provenance="source",
            ),
        ],
        ignored_boilerplate=[
            IgnoredBoilerplate(summary="Follow me on Instagram", role="boilerplate")
        ],
    )


def _ask(client: TestClient, location: str, step: int, **data) -> object:
    page = client.get(f"{location}/cook/{step}")
    assert page.status_code == 200
    payload = {"form_token": _token(page.text), **data}
    return client.post(
        f"{location}/cook/{step}/guide",
        data=payload,
        follow_redirects=True,
    )


def test_quick_actions_submit_predefined_questions(tmp_path: Path) -> None:
    guide = RecordingGuidance()
    client, guide = _client(tmp_path, _recipe(), guide)
    with client:
        location = _import(client)
        for key, label in QUICK_ACTIONS:
            before = len(guide.calls)
            page = _ask(client, location, 1, quick_action=key)
            assert page.status_code == 200
            assert label in page.text
            assert guide.calls[-1]["recent_messages"][-1]["text"] == label
            assert len(guide.calls) == before + 1
            assert quick_action_text(key) == label


def test_typed_followups_send_current_step_and_bounded_history(tmp_path: Path) -> None:
    guide = RecordingGuidance(
        replies=[
            "The recipe starts with garlic so the fat can take on its flavor. That is an explanation, not a new ingredient.",
            "Fragrant means it smells sweet and cooked, not raw. The recipe does not give a minute count.",
        ]
    )
    client, guide = _client(tmp_path, _recipe(), guide)
    with client:
        location = _import(client)
        original = _payload(client)
        first = _ask(client, location, 1, message="Why am I cooking the garlic first?")
        assert first.status_code == 200
        assert "Why am I cooking the garlic first?" in first.text
        assert "AI guidance" in first.text
        assert guide.calls[0]["current_step"]["number"] == 1
        assert guide.calls[0]["current_step"]["text"] == "Cook the garlic until fragrant."
        assert guide.calls[0]["title"] == "Garlic pasta"
        assert guide.calls[0]["recent_messages"][-1]["text"] == "Why am I cooking the garlic first?"
        second = _ask(client, location, 1, message="What does fragrant mean?")
        assert "What does fragrant mean?" in second.text
        recent = guide.calls[1]["recent_messages"]
        assert recent[0]["text"] == "Why am I cooking the garlic first?"
        assert recent[1]["role"] == "assistant"
        assert "not a new ingredient" in recent[1]["text"]
        assert len(recent) <= MAX_HISTORY_MESSAGES
        assert _payload(client) == original


def test_context_uses_edits_and_omits_rejected_material(tmp_path: Path) -> None:
    result = _recipe()
    result.ingredients.append(
        ExtractedIngredient(
            id="ing_asa",
            name="asafoetida",
            list_status="instruction_only",
            provenance="source",
        )
    )
    result.findings.append(
        ExtractionFinding(
            id="find_rejected",
            type="other",
            message="Do not add the mystery spice.",
            related_ids=["ing_garlic"],
        )
    )
    result.findings.append(
        ExtractionFinding(
            id="find_ing_asa",
            type="instruction_only_ingredient",
            message="Asafoetida is only in a step.",
            related_ids=["ing_asa"],
        )
    )
    result.findings.append(
        ExtractionFinding(
            id="find_missing_qty_ing_cream",
            type="missing_quantity",
            message="Cream amount is missing.",
            related_ids=["ing_cream"],
        )
    )
    result.ingredients[1].quantity = None
    result.ingredients[1].unit = None
    guide = RecordingGuidance()
    client, guide = _client(tmp_path, result, guide)
    with client:
        location = _import(client)
        original = _payload(client)
        edited = client.post(
            f"{location}/review/find_missing_qty_ing_cream",
            data={"action": "edit", "quantity": "3", "unit": "tablespoon"},
        )
        assert edited.status_code == 200
        rejected = client.post(
            f"{location}/review/find_ing_asa",
            data={"action": "reject"},
        )
        assert rejected.status_code == 200
        kept = client.post(
            f"{location}/review/find_rejected",
            data={"action": "reject"},
        )
        assert kept.status_code == 200
        page = _ask(client, location, 2, message="Will oat milk work?")
        assert page.status_code == 200
        context = guide.calls[0]
        encoded = json.dumps(context)
        assert context["current_step"]["text"] == "Stir in the cream."
        cream = next(item for item in context["ingredients"] if item["name"] == "heavy cream")
        assert cream["quantity"] == "3"
        assert cream["unit"] == "tablespoon"
        assert cream["quantity_provenance"] == "user_edit"
        assert "asafoetida" not in encoded.lower()
        assert "Do not add the mystery spice." not in encoded
        assert "Follow me on Instagram" not in encoded
        assert "Asafoetida is only in a step." not in encoded
        assert "sk-" not in encoded
        assert "/Users/" not in encoded
        assert "estimated_cost" not in encoded
        with client.app.state.db.connect() as conn:
            decisions = conn.execute("SELECT status FROM review_decisions").fetchall()
        assert [row["status"] for row in decisions] == ["edited", "rejected", "rejected"]
        assert _payload(client) == original
        assert "AI guidance" in page.text
        assert "suggestion" in page.text.lower() or "AI guidance" in page.text
        assert "source-backed" in GUIDANCE_INSTRUCTIONS or "not from the source" in GUIDANCE_INSTRUCTIONS


def test_history_is_isolated_by_recipe_step_and_session(tmp_path: Path) -> None:
    guide = RecordingGuidance()
    client, guide = _client(tmp_path, _recipe(), guide)
    with client:
        location = _import(client)
        _ask(client, location, 1, message="Why am I cooking the garlic first?")
        step_two = _ask(client, location, 2, message="Can I lower the heat?")
        assert "Why am I cooking the garlic first?" not in step_two.text
        assert "Can I lower the heat?" in step_two.text
        assert guide.calls[-1]["current_step"]["number"] == 2
        assert guide.calls[-1]["earlier_messages"][0]["step_number"] == 1
        again = _ask(client, location, 2, message="How much should I use?")
        third = _ask(client, location, 2, message="Can you explain that more simply?")
        assert "earlier_messages" not in guide.calls[-1]
        assert "How much should I use?" in third.text
        back = client.get(f"{location}/cook/1")
        assert "Why am I cooking the garlic first?" in back.text
        assert "Can I lower the heat?" not in back.text
        other = TestClient(client.app)
        assert other.get(f"{location}/cook/1").status_code == 404
        recipe_id = location.rstrip("/").split("/")[-1]
        assert client.app.state.db.list_cook_messages(recipe_id, "someone-else", 1) == []
        cookie = client.cookies.get("rca_session", "")
        assert "Why am I cooking the garlic first?" not in cookie


def test_storage_and_model_context_are_bounded(tmp_path: Path) -> None:
    guide = RecordingGuidance()
    client, guide = _client(tmp_path, _recipe(), guide)
    with client:
        location = _import(client)
        recipe_id = location.rstrip("/").split("/")[-1]
        session_id = client.get("/").cookies and None
        page = client.get(f"{location}/cook/1")
        assert page.status_code == 200
        with client.app.state.db.connect() as conn:
            owner = conn.execute(
                "SELECT session_id FROM extracted_recipes WHERE id = ?",
                (recipe_id,),
            ).fetchone()["session_id"]
        for index in range(6):
            client.app.state.db.append_cook_exchange(
                extraction_id=recipe_id,
                session_id=owner,
                step_number=1,
                user_text=f"question {index} " + ("salt " * 40),
                assistant_text=f"answer {index} " + ("heat " * 40),
                max_per_step=4,
                max_per_recipe=4,
            )
        stored = client.app.state.db.list_cook_messages(recipe_id, owner, 1)
        assert len(stored) == 4
        assert stored[0].body.startswith("question 4")
        working = _recipe()
        many = [
            {"role": "user" if index % 2 == 0 else "assistant", "text": f"turn {index}"}
            for index in range(30)
        ]
        context = build_model_context(
            working,
            step_number=1,
            current_messages=many,
            earlier_messages=[
                {"step_number": 2, "role": "user", "text": "old"},
                {"step_number": 2, "role": "assistant", "text": "older"},
                {"step_number": 2, "role": "user", "text": "oldest"},
            ],
        )
        assert len(context["recent_messages"]) == MAX_HISTORY_MESSAGES
        assert len(json.dumps(context)) <= MAX_CONTEXT_CHARS
        huge = working.model_copy(deep=True)
        huge.steps[0].text = "Simmer. " * 5000
        huge.creator_notes = []
        fitted = build_model_context(
            huge, step_number=1, current_messages=many, earlier_messages=[]
        )
        assert len(json.dumps(fitted)) <= MAX_CONTEXT_CHARS + 500
        assert fitted["current_step"].get("text_truncated") is True


def test_navigation_phrases_do_not_call_the_model(tmp_path: Path) -> None:
    guide = RecordingGuidance()
    client, guide = _client(tmp_path, _recipe(), guide)
    with client:
        location = _import(client)
        nxt = _ask(client, location, 1, message="next")
        assert "Step 2 of 2" in nxt.text
        assert guide.calls == []
        assert "cook-msg-ai" not in nxt.text
        back = _ask(client, location, 2, message="go back")
        assert "Step 1 of 2" in back.text
        repeat = _ask(client, location, 1, message="repeat this step")
        assert "Step 1 of 2" in repeat.text
        ambiguous = _ask(client, location, 1, message="What's next for the garlic?")
        assert len(guide.calls) == 1
        assert guide.calls[0]["current_step"]["number"] == 1
        finish = _ask(client, location, 1, message="finish cooking")
        assert "Cooking complete" in finish.text
        assert len(guide.calls) == 1
        again = client.post(
            f"{location}/cook",
            data={"action": "cook_again"},
            follow_redirects=True,
        )
        assert "Step 1 of 2" in again.text
        started = _ask(client, location, 1, message="start over")
        assert "Step 1 of 2" in started.text
        continued = _ask(client, location, 1, message="continue")
        assert "Step 2 of 2" in continued.text
        previous = _ask(client, location, 2, message="previous step")
        assert "Step 1 of 2" in previous.text
        done = _ask(client, location, 1, message="finish")
        assert "Cooking complete" in done.text
        assert len(guide.calls) == 1
        assert navigation_command("How do I finish cooking the garlic?") is None
        assert navigation_command("next time use less salt") is None


def test_button_navigation_still_matches_the_cursor(tmp_path: Path) -> None:
    guide = RecordingGuidance()
    client, _guide = _client(tmp_path, _recipe(), guide)
    with client:
        location = _import(client)
        first = client.get(f"{location}/cook", follow_redirects=True)
        assert "disabled" in first.text
        stayed = client.post(
            f"{location}/cook",
            data={"action": "back", "step": "1"},
            follow_redirects=True,
        )
        assert "Step 1 of 2" in stayed.text
        second = client.post(
            f"{location}/cook",
            data={"action": "next", "step": "1"},
            follow_redirects=True,
        )
        assert "Step 2 of 2" in second.text
        done = client.post(
            f"{location}/cook",
            data={"action": "finish", "step": "2"},
            follow_redirects=True,
        )
        assert "Cooking complete" in done.text


def test_rejected_inputs_do_not_call_or_store(tmp_path: Path) -> None:
    guide = RecordingGuidance()
    client, guide = _client(tmp_path, _recipe(), guide)
    with client:
        location = _import(client)
        original = _payload(client)
        empty = _ask(client, location, 1, message="   ")
        assert empty.status_code == 400
        assert "Enter a cooking question." in empty.text
        huge = _ask(client, location, 1, message="salt " * 300)
        assert huge.status_code == 400
        assert "too long" in huge.text
        invalid = _ask(client, location, 1, quick_action="not-real")
        assert invalid.status_code == 400
        assert "Choose a cooking question" in invalid.text
        assert guide.calls == []
        with client.app.state.db.connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM cook_messages").fetchone()["n"]
        assert count == 0
        assert _payload(client) == original


def test_rate_limit_blocks_before_the_client(tmp_path: Path) -> None:
    guide = RecordingGuidance()
    client, guide = _client(
        tmp_path,
        _recipe(),
        guide,
        guidance_limit_per_session=1,
        guidance_limit_per_process=10,
    )
    with client:
        location = _import(client)
        first = _ask(client, location, 1, message="Can I lower the heat?")
        assert first.status_code == 200
        blocked = _ask(client, location, 1, message="What should the sauce look like?")
        assert blocked.status_code == 429
        assert "Too many cooking questions" in blocked.text
        assert "Can I lower the heat?" in blocked.text
        assert len(guide.calls) == 1
        with client.app.state.db.connect() as conn:
            bodies = [
                row["body"]
                for row in conn.execute("SELECT body FROM cook_messages").fetchall()
            ]
        assert "What should the sauce look like?" not in bodies


def test_process_limit_is_separate_from_import_limit(tmp_path: Path) -> None:
    guide = RecordingGuidance()
    client, guide = _client(
        tmp_path,
        _recipe(),
        guide,
        guidance_limit_per_session=5,
        guidance_limit_per_process=1,
    )
    with client:
        location = _import(client)
        assert _ask(client, location, 1, message="Can I lower the heat?").status_code == 200
        other = TestClient(client.app)
        other_location = other.post(
            "/import",
            data={"recipe_text": "other"},
            follow_redirects=False,
        )
        assert other_location.status_code == 303
        other_url = other_location.headers["location"]
        page = other.get(f"{other_url}/cook/1")
        blocked = other.post(
            f"{other_url}/cook/1/guide",
            data={"form_token": _token(page.text), "message": "What heat should I use?"},
        )
        assert blocked.status_code == 429
        assert len(guide.calls) == 1
        assert client.app.state.guidance_limiter is not client.app.state.import_limiter


def test_model_failure_preserves_prior_state(tmp_path: Path) -> None:
    guide = RecordingGuidance(
        replies=["Fragrant means it smells cooked. The recipe does not give a time."],
        fail_after=1,
    )
    client, guide = _client(tmp_path, _recipe(), guide)
    with client:
        location = _import(client)
        original = _payload(client)
        first = _ask(client, location, 1, message="What does fragrant mean?")
        assert "Fragrant means" in first.text
        failed = _ask(client, location, 1, message="Mine is turning brown—is that bad?")
        assert failed.status_code == 502
        assert "unavailable right now" in failed.text
        assert "sk-testsecret" not in failed.text
        assert "/Users/nihal" not in failed.text
        assert "What does fragrant mean?" in failed.text
        assert "Mine is turning brown" in failed.text
        with client.app.state.db.connect() as conn:
            bodies = [row["body"] for row in conn.execute(
                "SELECT body FROM cook_messages ORDER BY id"
            ).fetchall()]
        assert bodies == [
            "What does fragrant mean?",
            "Fragrant means it smells cooked. The recipe does not give a time.",
        ]
        assert _payload(client) == original


def test_refresh_and_replay_do_not_duplicate(tmp_path: Path) -> None:
    guide = RecordingGuidance()
    client, guide = _client(tmp_path, _recipe(), guide)
    with client:
        location = _import(client)
        page = client.get(f"{location}/cook/1")
        token = _token(page.text)
        posted = client.post(
            f"{location}/cook/1/guide",
            data={"form_token": token, "message": "What did you mean by emulsify?"},
            follow_redirects=False,
        )
        assert posted.status_code == 303
        replay = client.post(
            f"{location}/cook/1/guide",
            data={"form_token": token, "message": "What did you mean by emulsify?"},
            follow_redirects=True,
        )
        assert replay.status_code == 200
        assert len(guide.calls) == 1
        shown = client.get(f"{location}/cook/1")
        assert shown.text.count("What did you mean by emulsify?") == 1
        assert shown.text.count("AI guidance") == 1


def test_empty_recipe_unresolved_expired_and_other_session(tmp_path: Path) -> None:
    pending = ExtractionResult(
        title="Oil",
        ingredients=[
            ExtractedIngredient(
                id="ing_oil",
                name="olive oil",
                list_status="listed",
                provenance="source",
            )
        ],
        steps=[
            ExtractedStep(id="step_1", text="Heat the pan.", provenance="source")
        ],
    )
    guide = RecordingGuidance()
    client, guide = _client(tmp_path, pending, guide)
    with client:
        location = _import(client)
        blocked = client.post(
            f"{location}/cook/1/guide",
            data={"message": "Can I lower the heat?", "form_token": "x"},
        )
        assert blocked.status_code == 400
        assert "Decide every review item" in blocked.text
        assert guide.calls == []

    empty = ExtractionResult(
        title="Plain",
        ingredients=[
            ExtractedIngredient(
                id="ing_bread",
                name="bread",
                quantity="1",
                unit="slice",
                list_status="listed",
                provenance="source",
            )
        ],
        steps=[],
    )
    guide = RecordingGuidance()
    client, guide = _client(tmp_path, empty, guide)
    with client:
        location = _import(client)
        missing = client.post(f"{location}/cook/1/guide", data={"message": "next"})
        assert missing.status_code == 400
        assert guide.calls == []

    guide = RecordingGuidance()
    client, guide = _client(tmp_path, _recipe(), guide)
    with client:
        location = _import(client)
        recipe_id = location.rstrip("/").split("/")[-1]
        client.get(f"{location}/cook/1")
        with client.app.state.db.connect() as conn:
            owner = conn.execute(
                "SELECT session_id FROM extracted_recipes WHERE id = ?",
                (recipe_id,),
            ).fetchone()["session_id"]
        client.app.state.db.append_cook_exchange(
            extraction_id=recipe_id,
            session_id=owner,
            step_number=1,
            user_text="Can I lower the heat?",
            assistant_text="Yes, as a suggestion.",
            max_per_step=4,
            max_per_recipe=4,
        )
        past = isoformat(utcnow() - timedelta(hours=2))
        with client.app.state.db.connect() as conn:
            conn.execute(
                "UPDATE extracted_recipes SET expires_at = ? WHERE id = ?",
                (past, recipe_id),
            )
            conn.execute(
                "UPDATE source_bundles SET expires_at = ?",
                (past,),
            )
        assert client.post(
            f"{location}/cook/1/guide",
            data={"message": "next", "form_token": "nope"},
        ).status_code == 404
        other = TestClient(client.app)
        assert other.get(f"{location}/cook/1").status_code == 404
        assert guide.calls == []
        removed = client.app.state.db.purge_expired()
        assert removed is not None
        with client.app.state.db.connect() as conn:
            left = conn.execute("SELECT COUNT(*) AS n FROM cook_messages").fetchone()["n"]
        assert left == 0


def test_invalid_step_does_not_move_a_valid_cursor(tmp_path: Path) -> None:
    guide = RecordingGuidance()
    client, guide = _client(tmp_path, _recipe(), guide)
    with client:
        location = _import(client)
        _ask(client, location, 2, message="What should the sauce look like?")
        invalid = client.post(
            f"{location}/cook/99/guide",
            data={"message": "next", "form_token": "bad"},
        )
        assert invalid.status_code == 400
        resume = client.get(f"{location}/cook", follow_redirects=False)
        assert resume.headers["location"].endswith("/cook/2")
        assert guide.calls and guide.calls[-1]["current_step"]["number"] == 2


def test_user_and_assistant_text_is_escaped(tmp_path: Path) -> None:
    guide = RecordingGuidance(replies=["<img src=x onerror=alert(1)>"])
    client, _guide = _client(tmp_path, _recipe(), guide)
    with client:
        location = _import(client)
        page = _ask(client, location, 1, message="<script>alert(1)</script>")
        assert "<script>alert(1)</script>" not in page.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
        assert "<img src=x onerror=alert(1)>" not in page.text
        assert "&lt;img src=x onerror=alert(1)&gt;" in page.text


def test_malformed_guidance_is_not_stored(tmp_path: Path) -> None:
    guide = RecordingGuidance(replies=["", "not-json"])
    client, guide = _client(tmp_path, _recipe(), guide)
    with client:
        location = _import(client)
        failed = _ask(client, location, 1, message="What heat should I use?")
        assert failed.status_code == 502
        assert guide.calls
        with client.app.state.db.connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM cook_messages").fetchone()["n"]
        assert count == 0


def test_parse_guidance_output_rejects_malformed_payloads() -> None:
    assert parse_guidance_output('{"guidance": "Use medium heat as a suggestion."}')
    with pytest.raises(Exception):
        parse_guidance_output("not json")
    with pytest.raises(Exception):
        parse_guidance_output('{"guidance": ""}')


def test_whitespace_and_navigation_do_not_consume_the_session_allowance(tmp_path: Path) -> None:
    guide = RecordingGuidance()
    client, guide = _client(
        tmp_path,
        _recipe(),
        guide,
        guidance_limit_per_session=1,
        guidance_limit_per_process=5,
    )
    with client:
        location = _import(client)
        assert _ask(client, location, 1, message="   ").status_code == 400
        moved = _ask(client, location, 1, message="next step")
        assert "Step 2 of 2" in moved.text
        asked = _ask(client, location, 2, message="I don’t have heavy cream. What can I use?")
        assert asked.status_code == 200
        assert len(guide.calls) == 1
