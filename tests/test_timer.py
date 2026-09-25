from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from recipe_cooking_assistant.app import create_app
from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.extraction import StaticExtractionClient
from recipe_cooking_assistant.models import ExtractedIngredient, ExtractedStep, ExtractionResult
from recipe_cooking_assistant.timer import (
    AmbiguousDurations,
    TimerClock,
    TimerOffer,
    parse_step_timer,
    storage_key,
)


def test_parses_explicit_durations_and_ignores_lookalikes() -> None:
    seconds = parse_step_timer("Cook the garlic for 30 seconds.")
    assert isinstance(seconds, TimerOffer)
    assert seconds.seconds == 30
    assert seconds.approximate is False
    minutes = parse_step_timer("Simmer 5 minutes.")
    assert isinstance(minutes, TimerOffer) and minutes.seconds == 300
    hours = parse_step_timer("Bake 1 hour.")
    assert isinstance(hours, TimerOffer) and hours.seconds == 3600
    about = parse_step_timer("Cook about 8 minutes.")
    assert isinstance(about, TimerOffer)
    assert about.approximate is True
    assert about.seconds == 480
    ranged = parse_step_timer("Roast 8–10 minutes.")
    assert isinstance(ranged, TimerOffer)
    assert ranged.seconds == 480
    assert ranged.range_label == "8–10 minutes"
    compound = parse_step_timer("Bake 1 hour and 30 minutes.")
    assert isinstance(compound, TimerOffer) and compound.seconds == 5400
    assert parse_step_timer("Heat the oven to 350°F.") is None
    assert parse_step_timer("Add 2 tablespoons oil to a 10-inch skillet.") is None
    assert parse_step_timer("Step 2. Add the salt.") is None
    many = parse_step_timer("Bake 25 minutes, then broil 3 minutes.")
    assert isinstance(many, AmbiguousDurations)
    assert len(many.labels) == 2


def test_clock_start_pause_resume_reset_refresh_and_isolation() -> None:
    clock = TimerClock("recipe-a", 1, 30_000, 30_000)
    clock.start(1_000)
    assert clock.view(11_000) == 20_000
    clock.pause(11_000)
    assert clock.running is False
    assert clock.view(50_000) == 20_000
    clock.resume(50_000)
    snap = clock.snapshot(60_000)
    restored = TimerClock.restore(snap, 65_000)
    assert restored.view(65_000) == 5_000
    assert snap["advances_step"] is False
    assert storage_key("recipe-a", 1) != storage_key("recipe-a", 2)
    assert storage_key("recipe-a", 1) != storage_key("recipe-b", 1)
    other = TimerClock("recipe-b", 2, 5_000, 5_000)
    other.start(0)
    clock.reset()
    assert clock.remaining_ms == 30_000
    assert clock.expired is False
    assert other.view(1_000) == 4_000
    clock.start(0)
    assert clock.view(30_000) == 0
    assert clock.expired is True
    assert clock.snapshot(31_000)["advances_step"] is False


def test_cook_page_offers_one_source_timer(tmp_path: Path) -> None:
    result = ExtractionResult(
        title="Skillet",
        servings="2",
        ingredients=[
            ExtractedIngredient(id="ing_oil", name="oil", quantity="1", unit="tablespoon", list_status="listed")
        ],
        steps=[
            ExtractedStep(id="step_1", text="Warm a 10-inch skillet to 350°F and cook for 5 minutes."),
            ExtractedStep(id="step_2", text="Bake 20 minutes, then rest 10 minutes."),
        ],
    )
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
        location = client.post("/import", data={"recipe_text": "skillet"}, follow_redirects=False).headers["location"]
        first = client.get(f"{location}/cook/1")
        assert "Start timer" in first.text
        assert "remaining" in first.text
        assert 'id="timer-toggle"' in first.text
        assert 'data-seconds="300"' in first.text
        assert "From this step" in first.text
        assert "timer.js" in first.text
        second = client.get(f"{location}/cook/2")
        assert "Start timer" not in second.text
        assert "more than one time" in second.text
        payload = client.get(location).text
        assert "1 tablespoon" in payload.lower()
