from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recipe_cooking_assistant.app import create_app
from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.extraction import StaticExtractionClient
from recipe_cooking_assistant.models import (
    ExtractedIngredient,
    ExtractedStep,
    ExtractionResult,
    ExtractionUsage,
    ReviewFlag,
    SourceEvidence,
)


TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00"
    b"\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
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


@pytest.fixture()
def sample_result() -> ExtractionResult:
    return ExtractionResult(
        insufficient_source=False,
        title="Test pasta",
        servings="2",
        ingredients=[
            ExtractedIngredient(
                id="ing_1",
                name="flour",
                quantity="1",
                unit="cup",
                provenance="source",
                confidence="high",
                evidence=SourceEvidence(quote="1 cup flour", image_index=None),
            )
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Mix and bake.",
                provenance="source",
                confidence="high",
                evidence=SourceEvidence(quote="Mix and bake.", image_index=None),
            )
        ],
    )


@pytest.fixture()
def client(tmp_path: Path, sample_result: ExtractionResult) -> TestClient:
    app = create_app(
        _settings(tmp_path),
        extraction_client=StaticExtractionClient(sample_result),
    )
    with TestClient(app) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_import_page_renders(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Recipe Cooking Assistant" in response.text
    assert "Extract recipe" in response.text


def test_import_requires_content(client: TestClient) -> None:
    response = client.post("/import", data={"recipe_text": ""})
    assert response.status_code == 400
    assert "Paste recipe text" in response.text


def test_import_extracts_and_shows_recipe(client: TestClient, tmp_path: Path) -> None:
    response = client.post(
        "/import",
        data={"recipe_text": "1 cup flour\nMix and bake."},
        files=[("images", ("recipe.png", io.BytesIO(TINY_PNG), "image/png"))],
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/recipes/")

    detail = client.get(location)
    assert detail.status_code == 200
    assert "Test pasta" in detail.text
    assert "flour" in detail.text
    assert "Source" in detail.text
    assert "Original source" in detail.text
    assert list(tmp_path.joinpath("uploads").rglob("*.png"))


def test_source_isolated_by_session(client: TestClient) -> None:
    create = client.post(
        "/import",
        data={"recipe_text": "secret family sauce"},
        follow_redirects=False,
    )
    location = create.headers["location"]

    other = TestClient(client.app)
    blocked = other.get(location)
    assert blocked.status_code == 404


def test_rejects_oversized_image(client: TestClient) -> None:
    settings = client.app.state.settings
    big = b"x" * (settings.max_upload_bytes + 1)
    response = client.post(
        "/import",
        data={"recipe_text": "optional text"},
        files=[("images", ("big.jpg", io.BytesIO(big), "image/jpeg"))],
    )
    assert response.status_code == 400
    assert "MB or smaller" in response.text


def test_insufficient_source_stays_on_import(tmp_path: Path) -> None:
    result = ExtractionResult(
        insufficient_source=True,
        insufficient_reason="Only a finished dish photo was provided.",
    )
    app = create_app(
        _settings(tmp_path),
        extraction_client=StaticExtractionClient(result),
    )
    with TestClient(app) as client:
        response = client.post(
            "/import",
            data={"recipe_text": ""},
            files=[("images", ("dish.png", io.BytesIO(TINY_PNG), "image/png"))],
        )
    assert response.status_code == 422
    assert "finished dish" in response.text.lower() or "Only a finished" in response.text


def test_missing_api_key_message(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), extraction_client=None)
    with TestClient(app) as client:
        response = client.post(
            "/import",
            data={"recipe_text": "1 cup sugar\nStir."},
        )
    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.text


def test_upload_order_preserved_in_bundle(tmp_path: Path, sample_result: ExtractionResult) -> None:
    app = create_app(
        _settings(tmp_path),
        extraction_client=StaticExtractionClient(sample_result),
    )
    with TestClient(app) as client:
        client.post(
            "/import",
            data={"recipe_text": "steps continue"},
            files=[
                ("images", ("a.png", io.BytesIO(TINY_PNG), "image/png")),
                ("images", ("b.png", io.BytesIO(TINY_PNG), "image/png")),
            ],
            follow_redirects=True,
        )
        db = client.app.state.db
        # Grab the only bundle via sqlite
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT filename, sort_index FROM source_images ORDER BY sort_index"
            ).fetchall()
        assert [r["filename"] for r in rows] == ["a.png", "b.png"]
        assert [r["sort_index"] for r in rows] == [0, 1]


def test_review_flags_render(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Ambiguous stew",
        ingredients=[
            ExtractedIngredient(
                id="ing_1",
                name="salt",
                quantity=None,
                provenance="needs_review",
                confidence="uncertain",
                evidence=SourceEvidence(quote="salt to taste"),
            )
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Simmer until done.",
                provenance="source",
                confidence="high",
            )
        ],
        review_flags=[
            ReviewFlag(
                type="uncertain_ordering",
                message="Step order across screenshots is unclear.",
                related_ids=["step_1"],
            )
        ],
    )
    app = create_app(
        _settings(tmp_path),
        extraction_client=StaticExtractionClient(result),
    )
    with TestClient(app) as client:
        response = client.post(
            "/import",
            data={"recipe_text": "salt to taste\nSimmer until done."},
            follow_redirects=True,
        )
    assert response.status_code == 200
    assert "Needs review" in response.text
    assert "uncertain ordering" in response.text
