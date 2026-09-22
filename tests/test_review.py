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
    ExtractionFinding,
    ExtractionResult,
    ReviewFlag,
    SourceEvidence,
)
from recipe_cooking_assistant.normalize import normalize_result


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


def _payload(client: TestClient) -> ExtractionResult:
    db = client.app.state.db
    with db.connect() as conn:
        row = conn.execute("SELECT payload_json FROM extracted_recipes").fetchone()
    assert row is not None
    return ExtractionResult.model_validate(json.loads(row["payload_json"]))


def test_missing_quantity_finding_skips_to_taste() -> None:
    result = ExtractionResult(
        title="Oil and salt",
        ingredients=[
            ExtractedIngredient(
                id="ing_oil",
                name="olive oil",
                quantity=None,
                list_status="listed",
                source_text="Olive oil",
            ),
            ExtractedIngredient(
                id="ing_salt",
                name="salt",
                quantity=None,
                notes="to taste",
                list_status="listed",
            ),
        ],
        steps=[ExtractedStep(id="step_1", text="Heat oil.")],
    )
    normalized = normalize_result(result, [])
    missing_ids = [
        related
        for finding in normalized.findings
        if finding.type == "missing_quantity"
        for related in finding.related_ids
    ]
    assert "ing_oil" in missing_ids
    assert "ing_salt" not in missing_ids


def test_add_to_ingredients_keeps_source_amount_and_payload(
    tmp_path: Path,
) -> None:
    result = ExtractionResult(
        title="Creamy Tomato Pasta",
        ingredients=[
            ExtractedIngredient(
                id="ing_1",
                name="penne",
                quantity="8",
                unit="ounces",
                list_status="listed",
                provenance="source",
            ),
            ExtractedIngredient(
                id="ing_garlic",
                name="garlic",
                list_status="instruction_only",
                provenance="source",
                evidence=SourceEvidence(quote="Add 2 cloves minced garlic"),
            ),
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Add 2 cloves minced garlic and cook.",
                provenance="source",
            )
        ],
    )
    with _client(tmp_path, result) as client:
        location = _import(client)
        page = client.get(location)
        assert page.status_code == 200
        assert "Add to ingredients" in page.text
        assert "Instruction only" in page.text

        posted = client.post(
            f"{location}/review/find_ing_garlic",
            data={"action": "accept"},
            follow_redirects=False,
        )
        assert posted.status_code == 303

        reviewed = client.get(location)
        assert reviewed.status_code == 200
        assert "Add to ingredients" not in reviewed.text
        assert "Accepted" in reviewed.text
        assert "2 cloves" in reviewed.text.lower() or "2 clove" in reviewed.text.lower()
        assert "AI suggestion" not in reviewed.text

        stored = _payload(client)
        garlic = next(item for item in stored.ingredients if item.id == "ing_garlic")
        assert garlic.list_status == "instruction_only"
        assert garlic.provenance == "source"


def test_instruction_only_edit_marks_changed_fields_only(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Garlic pasta",
        ingredients=[
            ExtractedIngredient(
                id="ing_garlic",
                name="garlic",
                list_status="instruction_only",
                evidence=SourceEvidence(quote="Add 2 cloves minced garlic"),
            )
        ],
        steps=[ExtractedStep(id="step_1", text="Add 2 cloves minced garlic.")],
    )
    with _client(tmp_path, result) as client:
        location = _import(client)
        posted = client.post(
            f"{location}/review/find_ing_garlic",
            data={
                "action": "edit",
                "name": "garlic",
                "quantity": "3",
                "unit": "cloves",
            },
            follow_redirects=True,
        )
        assert posted.status_code == 200
        assert "User edit" in posted.text
        assert "3 cloves" in posted.text.lower() or "3 clove" in posted.text.lower()

        stored = _payload(client)
        garlic = next(item for item in stored.ingredients if item.id == "ing_garlic")
        assert garlic.list_status == "instruction_only"
        assert garlic.quantity != "3"


def test_contradiction_step_amount_stays_source(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Cake",
        ingredients=[
            ExtractedIngredient(
                id="ing_1",
                name="flour",
                quantity="2",
                unit="cups",
                list_status="listed",
                provenance="source",
                evidence=SourceEvidence(quote="2 cups flour"),
            )
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Mix 3 cups flour with water.",
                related_ingredient_ids=["ing_1"],
                evidence=SourceEvidence(quote="Mix 3 cups flour with water."),
            )
        ],
    )
    with _client(tmp_path, result) as client:
        location = _import(client)
        page = client.get(location)
        assert "Keep listed amount" in page.text

        finding_id = "find_contradiction_ing_1_step_1"
        posted = client.post(
            f"{location}/review/{finding_id}",
            data={"action": "edit", "choice": "step"},
            follow_redirects=True,
        )
        assert posted.status_code == 200
        assert "3 cups flour" in posted.text or "3 cups" in posted.text
        assert "User edit" not in posted.text

        stored = _payload(client)
        flour = next(item for item in stored.ingredients if item.id == "ing_1")
        assert flour.quantity == "2"
        assert flour.unit == "cups"


def test_contradiction_custom_amount_is_user_edit(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Cake",
        ingredients=[
            ExtractedIngredient(
                id="ing_1",
                name="flour",
                quantity="2",
                unit="cups",
                list_status="listed",
                provenance="source",
            )
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Mix 3 cups flour with water.",
                related_ingredient_ids=["ing_1"],
            )
        ],
    )
    with _client(tmp_path, result) as client:
        location = _import(client)
        posted = client.post(
            f"{location}/review/find_contradiction_ing_1_step_1",
            data={
                "action": "edit",
                "choice": "custom",
                "quantity": "2.5",
                "unit": "cups",
            },
            follow_redirects=True,
        )
        assert posted.status_code == 200
        assert "User edit" in posted.text
        assert "2.5" in posted.text

        stored = _payload(client)
        flour = next(item for item in stored.ingredients if item.id == "ing_1")
        assert flour.quantity == "2"


def test_reject_does_not_apply_and_stays_visible(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Oil",
        ingredients=[
            ExtractedIngredient(
                id="ing_oil",
                name="olive oil",
                quantity=None,
                list_status="listed",
                source_text="Olive oil",
            )
        ],
        steps=[ExtractedStep(id="step_1", text="Heat oil.")],
    )
    with _client(tmp_path, result) as client:
        location = _import(client)
        page = client.get(location)
        assert "Keep unspecified" in page.text

        posted = client.post(
            f"{location}/review/find_missing_qty_ing_oil",
            data={"action": "reject"},
            follow_redirects=True,
        )
        assert posted.status_code == 200
        assert "Rejected" in posted.text
        assert "Keep unspecified" not in posted.text
        assert "Olive oil" in posted.text


def test_edit_missing_quantity_applies_and_renders_amount(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Creamy Tomato Pasta",
        ingredients=[
            ExtractedIngredient(
                id="ing_oil",
                name="olive oil",
                quantity=None,
                list_status="listed",
                source_text="Olive oil",
                provenance="source",
                evidence=SourceEvidence(quote="Olive oil"),
            ),
            ExtractedIngredient(
                id="ing_garlic",
                name="garlic cloves",
                quantity="2",
                unit="cloves",
                list_status="instruction_only",
                source_text="2 cloves minced garlic",
                provenance="source",
                evidence=SourceEvidence(quote="Add 2 cloves minced garlic"),
            ),
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Add 2 cloves minced garlic. Heat oil.",
                provenance="source",
            )
        ],
    )
    with _client(tmp_path, result) as client:
        location = _import(client)
        garlic = client.post(
            f"{location}/review/find_ing_garlic",
            data={"action": "accept"},
            follow_redirects=True,
        )
        assert garlic.status_code == 200
        assert "Added to ingredients" in garlic.text
        assert "2 cloves" in garlic.text.lower()

        edited = client.post(
            f"{location}/review/find_missing_qty_ing_oil",
            data={"action": "edit", "quantity": "1", "unit": "tablespoon"},
            follow_redirects=True,
        )
        assert edited.status_code == 200
        assert "Set to 1 tablespoon" in edited.text
        assert "1 tablespoon" in edited.text.lower()
        assert "User edit" in edited.text
        assert "2 cloves" in edited.text.lower()
        assert "Added to ingredients" in edited.text

        stored = _payload(client)
        oil = next(item for item in stored.ingredients if item.id == "ing_oil")
        assert oil.quantity is None
        assert oil.unit is None
        assert oil.source_text == "Olive oil"

        db = client.app.state.db
        with db.connect() as conn:
            row = conn.execute(
                """
                SELECT status, resolution_json FROM review_decisions
                WHERE finding_id = ?
                """,
                ("find_missing_qty_ing_oil",),
            ).fetchone()
        assert row is not None
        assert row["status"] == "edited"
        resolution = json.loads(row["resolution_json"])
        assert resolution["quantity"] == "1"
        assert resolution["unit"] == "tablespoon"


def test_invalid_review_action_is_rejected(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Oil",
        ingredients=[
            ExtractedIngredient(
                id="ing_oil",
                name="olive oil",
                quantity=None,
                list_status="listed",
            )
        ],
        steps=[ExtractedStep(id="step_1", text="Heat oil.")],
    )
    with _client(tmp_path, result) as client:
        location = _import(client)
        response = client.post(
            f"{location}/review/find_missing_qty_ing_oil",
            data={"action": "explode"},
        )
        assert response.status_code == 400
        assert "valid review action" in response.text.lower()


def test_review_isolated_by_session(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Oil",
        ingredients=[
            ExtractedIngredient(
                id="ing_oil",
                name="olive oil",
                quantity=None,
                list_status="listed",
            )
        ],
        steps=[ExtractedStep(id="step_1", text="Heat oil.")],
    )
    with _client(tmp_path, result) as client:
        location = _import(client)
        other = TestClient(client.app)
        blocked = other.post(
            f"{location}/review/find_missing_qty_ing_oil",
            data={"action": "accept"},
        )
        assert blocked.status_code == 404


def _creamy_tomato_duplicate_garlic() -> ExtractionResult:
    """Exact live Creamy Tomato shape: model finding + deterministic finding."""
    evidence = SourceEvidence(
        quote="Add 2 cloves minced garlic and cook for 30 seconds."
    )
    return ExtractionResult(
        title="Creamy Tomato Pasta",
        ingredients=[
            ExtractedIngredient(
                id="ing_1",
                name="penne",
                quantity="8",
                unit="ounces",
                list_status="listed",
                provenance="source",
            ),
            ExtractedIngredient(
                id="ing_2",
                name="olive oil",
                quantity=None,
                list_status="listed",
                source_text="Olive oil",
                evidence=SourceEvidence(quote="Olive oil"),
            ),
            ExtractedIngredient(
                id="ing_6",
                name="garlic cloves",
                quantity="2",
                unit="cloves",
                list_status="instruction_only",
                provenance="source",
                source_text="2 cloves minced garlic",
                evidence=evidence,
            ),
        ],
        steps=[
            ExtractedStep(
                id="step_3",
                text="Add 2 cloves minced garlic and cook for 30 seconds.",
                related_ingredient_ids=["ing_6"],
                evidence=evidence,
            ),
            ExtractedStep(
                id="step_6",
                text=(
                    "Toss the sauce with the cooked pasta and "
                    "finish with grated Parmesan."
                ),
                related_ingredient_ids=["ing_1"],
                evidence=SourceEvidence(
                    quote="Toss with the pasta and finish with grated Parmesan."
                ),
            ),
        ],
        review_flags=[
            ReviewFlag(
                type="instruction_only_ingredient",
                message=(
                    "Referenced in instructions but absent from ingredient list."
                ),
                related_ids=["ing_6"],
            )
        ],
        findings=[
            ExtractionFinding(
                id="find_1",
                type="instruction_only_ingredient",
                message=(
                    "Referenced in instructions but absent from ingredient list."
                ),
                related_ids=["ing_6"],
                evidence=evidence,
            ),
            ExtractionFinding(
                id="find_ing_6",
                type="instruction_only_ingredient",
                message="Referenced in instructions but absent from ingredient list",
                related_ids=["ing_6"],
                evidence=evidence,
            ),
        ],
    )


def test_duplicate_garlic_findings_collapse_to_one() -> None:
    normalized = normalize_result(_creamy_tomato_duplicate_garlic(), [])
    garlic_findings = [
        finding
        for finding in normalized.findings
        if finding.type == "instruction_only_ingredient" and "ing_6" in finding.related_ids
    ]
    assert len(garlic_findings) == 1
    kept = garlic_findings[0]
    assert kept.evidence is not None
    assert "2 cloves minced garlic" in (kept.evidence.quote or "")
    assert {kept.id, *kept.alias_ids} >= {"find_1", "find_ing_6"}
    assert not any("parmesan" in item.name.lower() for item in normalized.ingredients)


def test_duplicate_garlic_review_queue_is_single_card(tmp_path: Path) -> None:
    with _client(tmp_path, _creamy_tomato_duplicate_garlic()) as client:
        location = _import(client)
        page = client.get(location)
        assert page.status_code == 200
        assert page.text.count("Add to ingredients") == 1
        assert page.text.lower().count("instruction only") >= 1

        first = client.post(
            f"{location}/review/find_1",
            data={"action": "accept"},
            follow_redirects=True,
        )
        assert first.status_code == 200
        assert first.text.count("Accepted") == 1

        second = client.post(
            f"{location}/review/find_ing_6",
            data={"action": "reject"},
            follow_redirects=True,
        )
        assert second.status_code == 200
        assert second.text.count("Rejected") == 1
        assert second.text.count("Accepted") == 0
        assert second.text.count("Add to ingredients") == 0

        db = client.app.state.db
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT finding_id, status FROM review_decisions"
            ).fetchall()
        assert len(rows) == 1


def test_duplicate_findings_keep_stronger_evidence() -> None:
    result = ExtractionResult(
        title="Garlic paste",
        ingredients=[
            ExtractedIngredient(
                id="ing_6",
                name="garlic cloves",
                list_status="instruction_only",
            )
        ],
        steps=[ExtractedStep(id="step_1", text="Add garlic.")],
        findings=[
            ExtractionFinding(
                id="flag_0_instruction_only_ingredient",
                type="instruction_only_ingredient",
                message="Referenced in instructions but absent from ingredient list",
                related_ids=["ing_6"],
                evidence=None,
            ),
            ExtractionFinding(
                id="find_1",
                type="instruction_only_ingredient",
                message="Referenced in instructions but absent from ingredient list.",
                related_ids=["ing_6"],
                evidence=SourceEvidence(
                    quote="Add 2 cloves minced garlic and cook for 30 seconds."
                ),
            ),
        ],
    )
    normalized = normalize_result(result, [])
    garlic_findings = [
        finding
        for finding in normalized.findings
        if finding.type == "instruction_only_ingredient"
    ]
    assert len(garlic_findings) == 1
    assert garlic_findings[0].evidence is not None
    assert "2 cloves minced garlic" in (garlic_findings[0].evidence.quote or "")
