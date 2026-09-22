from __future__ import annotations

from recipe_cooking_assistant.models import (
    ExtractedIngredient,
    ExtractedStep,
    ExtractionFinding,
    ExtractionResult,
    ReviewFlag,
    SourceEvidence,
)
from recipe_cooking_assistant.normalize import normalize_result
from recipe_cooking_assistant.quantity_consistency import (
    amounts_conflict,
    apply_quantity_consistency_checks,
    extract_step_amount_for_ingredient,
)


def test_two_cups_vs_three_cups_becomes_contradiction() -> None:
    """Exact live-failure shape: listed 2 cups, step 3 cups, wrong finding type."""
    result = ExtractionResult(
        title="Cake",
        ingredients=[
            ExtractedIngredient(
                id="ing_1",
                name="flour",
                quantity="2",
                unit="cups",
                source_text="2 cups flour",
                list_status="listed",
                evidence=SourceEvidence(quote="2 cups flour"),
            )
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Mix 3 cups flour with water.",
                related_ingredient_ids=["ing_1"],
                evidence=SourceEvidence(quote="Mix 3 cups flour with water."),
                source_direction_text="Mix 3 cups flour with water.",
            )
        ],
        findings=[
            ExtractionFinding(
                id="find_1",
                type="instruction_only_ingredient",
                message=(
                    "Referenced in instructions but absent from ingredient list: "
                    "3 cups flour."
                ),
                related_ids=["ing_1"],
                evidence=SourceEvidence(quote="Mix 3 cups flour with water."),
            )
        ],
        review_flags=[
            ReviewFlag(
                type="instruction_only_ingredient",
                message=(
                    "Referenced in instructions but absent from ingredient list: "
                    "3 cups flour."
                ),
                related_ids=["ing_1"],
            )
        ],
    )

    normalized = normalize_result(result, [])
    assert not any(
        f.type == "instruction_only_ingredient" for f in normalized.findings
    )
    contradictions = [f for f in normalized.findings if f.type == "contradiction"]
    assert len(contradictions) == 1
    message = contradictions[0].message.lower()
    assert "2" in message and "3" in message
    assert "cup" in message
    assert contradictions[0].evidence is not None
    assert "3 cups flour" in (contradictions[0].evidence.quote or "")


def test_matching_quantities_are_not_contradictions() -> None:
    result = ExtractionResult(
        ingredients=[
            ExtractedIngredient(
                id="ing_1",
                name="flour",
                quantity="2",
                unit="cups",
                list_status="listed",
            )
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Mix 2 cups flour with water.",
                related_ingredient_ids=["ing_1"],
            )
        ],
    )
    apply_quantity_consistency_checks(result)
    assert not any(f.type == "contradiction" for f in result.findings)


def test_ambiguous_some_flour_is_not_a_contradiction() -> None:
    result = ExtractionResult(
        ingredients=[
            ExtractedIngredient(
                id="ing_1",
                name="flour",
                quantity="2",
                unit="cups",
                list_status="listed",
            )
        ],
        steps=[
            ExtractedStep(
                id="step_1",
                text="Add some flour to the bowl.",
                related_ingredient_ids=["ing_1"],
            )
        ],
    )
    apply_quantity_consistency_checks(result)
    assert extract_step_amount_for_ingredient(
        "Add some flour to the bowl.", result.ingredients[0]
    ) is None
    assert not any(f.type == "contradiction" for f in result.findings)


def test_incompatible_units_are_not_forced() -> None:
    assert not amounts_conflict("2", "cups", "3", "tablespoons")


def test_duplicate_contradictions_not_added() -> None:
    result = ExtractionResult(
        ingredients=[
            ExtractedIngredient(
                id="ing_1",
                name="flour",
                quantity="2",
                unit="cup",
                list_status="listed",
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
    apply_quantity_consistency_checks(result)
    apply_quantity_consistency_checks(result)
    assert len([f for f in result.findings if f.type == "contradiction"]) == 1
