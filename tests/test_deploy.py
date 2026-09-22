from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recipe_cooking_assistant.app import create_app
from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.db import Database, isoformat, utcnow
from recipe_cooking_assistant.extraction import (
    StaticExtractionClient,
    sanitize_error_message,
)
from recipe_cooking_assistant.import_limit import request_exceeds_upload_budget
from recipe_cooking_assistant.models import (
    ExtractedIngredient,
    ExtractedStep,
    ExtractionResult,
)
from recipe_cooking_assistant.runtime import (
    ProductionConfigError,
    serve_host,
    serve_port,
)
from recipe_cooking_assistant.storage import delete_paths

ROOT = Path(__file__).resolve().parents[1]

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00"
    b"\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "session_secret": "test-secret",
        "data_dir": tmp_path,
        "openai_api_key": None,
        "openai_model": "gpt-4.1-mini",
        "max_images": 6,
        "max_upload_bytes": 4 * 1024 * 1024,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_health_does_not_require_an_api_key(tmp_path: Path) -> None:
    app = create_app(
        _settings(tmp_path),
        extraction_client=StaticExtractionClient(ExtractionResult(title="Unused")),
    )
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "sk-" not in response.text


def test_local_bind_stays_on_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    assert serve_host() == "127.0.0.1"
    assert serve_port() == 8000


def test_render_binds_all_interfaces_and_supplied_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("PORT", "10000")
    assert serve_host() == "0.0.0.0"
    assert serve_port() == 10000


def test_render_refuses_to_start_without_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProductionConfigError) as caught:
        create_app(_settings(tmp_path))
    message = str(caught.value)
    assert "SESSION_SECRET" in message
    assert "OPENAI_API_KEY" in message
    assert "sk-" not in message


def test_render_rejects_placeholder_session_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("SESSION_SECRET", "dev-only-change-me")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    with pytest.raises(ProductionConfigError) as caught:
        create_app(_settings(tmp_path))
    assert caught.value.missing == ["SESSION_SECRET"]


def test_render_starts_when_env_names_are_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    app = create_app(_settings(tmp_path))
    session = next(
        layer
        for layer in app.user_middleware
        if layer.cls.__name__ == "SessionMiddleware"
    )
    assert session.kwargs["https_only"] is True
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_import_limit_blocks_further_paid_calls(tmp_path: Path) -> None:
    class CountingClient(StaticExtractionClient):
        def __init__(self) -> None:
            super().__init__(
                ExtractionResult(
                    title="Toast",
                    ingredients=[
                        ExtractedIngredient(
                            id="ing_bread",
                            name="bread",
                            quantity="2",
                            unit="slices",
                        )
                    ],
                    steps=[ExtractedStep(id="step_1", text="Toast the bread.")],
                )
            )
            self.calls = 0

        def extract(self, *, bundle, settings):  # type: ignore[no-untyped-def]
            self.calls += 1
            return super().extract(bundle=bundle, settings=settings)

    extractor = CountingClient()
    app = create_app(
        _settings(
            tmp_path,
            import_limit_per_client=1,
            import_limit_per_process=1,
        ),
        extraction_client=extractor,
    )
    with TestClient(app) as client:
        first = client.post(
            "/import",
            data={"recipe_text": "Toast the bread."},
            follow_redirects=False,
        )
        second = client.post(
            "/import",
            data={"recipe_text": "Toast the bread again."},
            follow_redirects=False,
        )
    assert first.status_code == 303
    assert second.status_code == 429
    assert "few recipes per hour" in second.text
    assert extractor.calls == 1


def test_upload_budget_uses_existing_image_limits() -> None:
    class Dummy:
        headers = {"content-length": str(6 * 4 * 1024 * 1024 + 256 * 1024 + 1)}

    assert request_exceeds_upload_budget(
        Dummy(),  # type: ignore[arg-type]
        max_images=6,
        max_upload_bytes=4 * 1024 * 1024,
    )
    small = Dummy()
    small.headers = {"content-length": "1000"}
    assert not request_exceeds_upload_budget(
        small,  # type: ignore[arg-type]
        max_images=6,
        max_upload_bytes=4 * 1024 * 1024,
    )


def test_expired_uploads_are_deleted_and_ignored(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    stored = tmp_path / "uploads" / "session" / "bundle" / "pic.png"
    stored.parent.mkdir(parents=True)
    stored.write_bytes(TINY_PNG)
    db = Database(settings.database_path)
    db.init_schema()
    db.create_bundle(
        session_id="session",
        raw_text="toast",
        settings=settings,
        images=[
            ("img", "pic.png", str(stored), "image/png", len(TINY_PNG), 0),
        ],
    )
    past = isoformat(utcnow() - timedelta(hours=2))
    with db.connect() as conn:
        conn.execute("UPDATE source_bundles SET expires_at = ?", (past,))
    delete_paths(db.purge_expired())
    assert not stored.exists()

    ignored = (ROOT / ".gitignore").read_text()
    assert "uploads/" in ignored
    assert "*.db" in ignored
    assert ".env" in ignored


def test_unhandled_errors_hide_internals(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("sk-abc1234567890xyz /Users/nihal/secret recipe text")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")
    assert response.status_code == 500
    assert "Traceback" not in response.text
    assert "sk-" not in response.text
    assert "/Users" not in response.text
    assert "recipe text" not in response.text
    assert "Something went wrong" in response.text


def test_logs_redact_paths_and_keys() -> None:
    cleaned = sanitize_error_message(
        RuntimeError("failed sk-abc1234567890xyz at /Users/nihal/secret/app.db")
    )
    assert "sk-abc" not in cleaned
    assert "/Users/nihal" not in cleaned
    assert "[redacted-path]" in cleaned
    assert "[redacted-api-key]" in cleaned


def test_unknown_path_stays_not_found(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/missing")
    assert response.status_code == 404
    assert "Traceback" not in response.text
