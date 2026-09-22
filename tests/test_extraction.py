from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.db import SourceBundle, SourceImage
from recipe_cooking_assistant.extraction import (
    SAFE_UI_API_ERROR,
    ExtractionError,
    OpenAIExtractionClient,
    build_user_content,
    extract_request_id,
    normalize_result,
    sanitize_error_message,
)
from recipe_cooking_assistant.extraction_schema import (
    EXTRACTION_JSON_SCHEMA,
    SYSTEM_PROMPT,
)
from recipe_cooking_assistant.models import (
    ExtractedIngredient,
    ExtractedStep,
    ExtractionResult,
    SourceEvidence,
)


def test_system_prompt_covers_messy_source_rules() -> None:
    assert "UPLOAD ORDER" in SYSTEM_PROMPT
    assert "insufficient_source" in SYSTEM_PROMPT
    assert "content_roles" in SYSTEM_PROMPT
    assert "ignored_boilerplate" in SYSTEM_PROMPT
    assert "needs_review" in SYSTEM_PROMPT


def test_build_user_content_orders_images_by_sort_index(tmp_path: Path) -> None:
    paths = []
    for name in ("second.png", "first.png"):
        path = tmp_path / name
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00"
            b"\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        paths.append(path)

    bundle = SourceBundle(
        id="b1",
        session_id="s1",
        raw_text="caption",
        created_at="t",
        expires_at="t",
        images=[
            SourceImage(
                id="img_b",
                bundle_id="b1",
                session_id="s1",
                filename="second.png",
                stored_path=str(paths[0]),
                content_type="image/png",
                size_bytes=10,
                sort_index=1,
                created_at="t",
            ),
            SourceImage(
                id="img_a",
                bundle_id="b1",
                session_id="s1",
                filename="first.png",
                stored_path=str(paths[1]),
                content_type="image/png",
                size_bytes=10,
                sort_index=0,
                created_at="t",
            ),
        ],
    )
    parts = build_user_content(bundle)
    assert parts[0]["type"] == "input_text"
    text = parts[0]["text"]
    assert text.index("image_index=0") < text.index("image_index=1")
    image_parts = [p for p in parts if p.get("type") == "input_image"]
    assert len(image_parts) == 2
    assert "detail" in image_parts[0]
    markers = [
        p["text"]
        for p in parts
        if p.get("type") == "input_text" and "End of screenshot" in p.get("text", "")
    ]
    assert "image_index=0" in markers[0]
    assert "image_index=1" in markers[1]


def test_normalize_fills_image_id_and_promotes_uncertain() -> None:
    images = [
        SourceImage(
            id="img_9",
            bundle_id="b",
            session_id="s",
            filename="a.png",
            stored_path="/tmp/a.png",
            content_type="image/png",
            size_bytes=1,
            sort_index=0,
            created_at="t",
        )
    ]
    result = ExtractionResult(
        ingredients=[
            ExtractedIngredient(
                id="ing_1",
                name="butter",
                provenance="source",
                confidence="uncertain",
                evidence=SourceEvidence(quote="butter", image_index=0),
            )
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Melt butter.",
                provenance="source",
                confidence="high",
                evidence=SourceEvidence(image_index=0),
            )
        ],
    )
    normalized = normalize_result(result, images)
    assert normalized.ingredients[0].provenance == "needs_review"
    assert normalized.ingredients[0].evidence is not None
    assert normalized.ingredients[0].evidence.image_id == "img_9"
    assert normalized.steps[0].evidence is not None
    assert normalized.steps[0].evidence.image_id == "img_9"


def test_sanitize_error_message_redacts_secrets_and_images() -> None:
    raw = (
        "Permission denied for key sk-abc1234567890xyz "
        "payload data:image/png;base64,AAAA and more"
    )
    cleaned = sanitize_error_message(Exception(raw))
    assert "sk-abc" not in cleaned
    assert "[redacted-api-key]" in cleaned
    assert "AAAA" not in cleaned
    assert "[redacted-image-data]" in cleaned


def test_extract_request_id_from_api_error() -> None:
    exc = SimpleNamespace(request_id="req_test_123")
    assert extract_request_id(exc) == "req_test_123"  # type: ignore[arg-type]


def test_openai_client_uses_responses_api_not_chat_completions() -> None:
    mock_client = MagicMock()
    mock_client.responses.create.return_value = SimpleNamespace(
        output_text=(
            '{"insufficient_source":false,"insufficient_reason":null,'
            '"title":"Soup","servings":"2","ingredients":[],"steps":'
            '[{"id":"step_1","text":"Simmer.","provenance":"source",'
            '"confidence":"high","evidence":null,"related_ingredient_ids":[],'
            '"source_direction_text":null}],'
            '"creator_notes":[],"content_roles":[],"review_flags":[],'
            '"findings":[],"ignored_boilerplate":[]}'
        ),
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        id="resp_123",
    )
    # Ensure chat path is never used
    mock_client.chat.completions.create.side_effect = AssertionError(
        "chat.completions must not be called"
    )

    settings = Settings(
        session_secret="test",
        openai_api_key="sk-test",
        openai_model="gpt-4.1-mini",
    )
    bundle = SourceBundle(
        id="bundle-1",
        session_id="session-1",
        raw_text="Simmer soup.",
        created_at="t",
        expires_at="t",
        images=[],
    )
    client = OpenAIExtractionClient(api_key="sk-test", client=mock_client)
    result, usage = client.extract(bundle=bundle, settings=settings)

    mock_client.responses.create.assert_called_once()
    mock_client.chat.completions.create.assert_not_called()
    kwargs = mock_client.responses.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4.1-mini"
    assert kwargs["instructions"] == SYSTEM_PROMPT
    assert kwargs["text"]["format"]["type"] == "json_schema"
    assert kwargs["text"]["format"]["schema"] == EXTRACTION_JSON_SCHEMA
    assert kwargs["text"]["format"]["strict"] is True
    assert kwargs["input"][0]["role"] == "user"
    assert kwargs["input"][0]["content"][0]["type"] == "input_text"
    assert result.title == "Soup"
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50


def test_openai_client_api_failure_logs_and_raises_safe_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_client = MagicMock()

    class FakeAPIError(Exception):
        request_id = "req_fail_9"

        def __str__(self) -> str:
            return "403 Forbidden for sk-supersecretkey999 and data:image/png;base64,AAA"

    mock_client.responses.create.side_effect = FakeAPIError()
    settings = Settings(session_secret="test", openai_model="gpt-4.1-mini")
    bundle = SourceBundle(
        id="bundle-err",
        session_id="s",
        raw_text="x",
        created_at="t",
        expires_at="t",
        images=[],
    )
    client = OpenAIExtractionClient(api_key="sk-test", client=mock_client)

    with caplog.at_level("ERROR"), pytest.raises(ExtractionError) as raised:
        client.extract(bundle=bundle, settings=settings)

    assert raised.value.message == SAFE_UI_API_ERROR
    assert "FakeAPIError" in caplog.text
    assert "req_fail_9" in caplog.text
    assert "bundle-err" in caplog.text
    assert "sk-supersecretkey999" not in caplog.text
    assert "AAA" not in caplog.text
