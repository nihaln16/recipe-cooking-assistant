from __future__ import annotations

from typing import Any

from recipe_cooking_assistant.ingredient_display import format_ingredient_line
from recipe_cooking_assistant.models import ExtractedIngredient, ExtractionResult


class AssertionFailure(AssertionError):
    """Raised when one or more semantic assertions fail."""

    def __init__(self, case_id: str, failures: list[str]) -> None:
        self.case_id = case_id
        self.failures = failures
        joined = "; ".join(failures)
        super().__init__(f"[{case_id}] {len(failures)} failure(s): {joined}")


def _find_ingredient(
    ingredients: list[ExtractedIngredient], name_substring: str
) -> ExtractedIngredient | None:
    needle = name_substring.lower()
    return next((i for i in ingredients if needle in i.name.lower()), None)


def collect_semantic_failures(
    case_id: str, result: ExtractionResult, expect: dict[str, Any]
) -> list[str]:
    """Return all semantic assertion failures (does not raise)."""
    failures: list[str] = []

    def fail(message: str) -> None:
        failures.append(message)

    if "insufficient_source" in expect:
        if result.insufficient_source != bool(expect["insufficient_source"]):
            fail(
                f"insufficient_source expected {expect['insufficient_source']}, "
                f"got {result.insufficient_source}"
            )

    if expect.get("insufficient_source") is True:
        if result.ingredients or result.steps:
            fail("insufficient_source should clear ingredients/steps")
        return failures

    listed = result.listed_ingredients()
    listed_names = [i.name.lower() for i in listed]
    listed_lines = [format_ingredient_line(i).lower() for i in listed]
    instruction_only = result.instruction_only_ingredients()

    for needle in expect.get("listed_name_substrings", []):
        if not any(needle.lower() in n for n in listed_names):
            fail(f"expected listed ingredient containing {needle!r}")

    for needle in expect.get("listed_name_exclude_substrings", []):
        if any(needle.lower() in n for n in listed_names):
            fail(f"listed ingredients must not include {needle!r}")

    for needle in expect.get("instruction_only_name_substrings", []):
        if not any(needle.lower() in i.name.lower() for i in instruction_only):
            fail(f"expected instruction_only ingredient containing {needle!r}")

    for ftype in expect.get("finding_types", []):
        if not any(f.type == ftype for f in result.findings):
            fail(f"expected finding type {ftype!r}")

    for ftype in expect.get("finding_types_exclude", []):
        if any(f.type == ftype for f in result.findings):
            fail(f"must not have finding type {ftype!r}")

    if expect.get("no_quantity_for_instruction_only"):
        for item in instruction_only:
            if not item.quantity:
                continue
            evidence = " ".join(
                part
                for part in (
                    item.evidence.quote if item.evidence else None,
                    item.source_text,
                    item.notes,
                )
                if part
            ).lower()
            if item.quantity.lower() not in evidence:
                fail(
                    f"instruction_only {item.name!r} quantity {item.quantity!r} "
                    "is not present in source evidence"
                )

    for check in expect.get("ingredient_checks", []):
        match = _find_ingredient(result.ingredients, check["name_substring"])
        if match is None:
            fail(f"missing ingredient matching {check['name_substring']!r}")
            continue
        if check.get("quantity_is_null") and match.quantity is not None:
            fail(f"{match.name} quantity should be null")
        if "quantity_substring" in check:
            qty = (match.quantity or "").lower()
            if check["quantity_substring"].lower() not in qty:
                fail(
                    f"{match.name} quantity expected to contain "
                    f"{check['quantity_substring']!r}, got {match.quantity!r}"
                )
        if "unit_substring" in check:
            unit = (match.unit or "").lower()
            if check["unit_substring"].lower() not in unit:
                fail(
                    f"{match.name} unit expected to contain "
                    f"{check['unit_substring']!r}, got {match.unit!r}"
                )
        if "optional" in check and match.optional != bool(check["optional"]):
            fail(f"{match.name} optional expected {check['optional']}")
        if "list_status" in check and match.list_status != check["list_status"]:
            fail(f"{match.name} list_status expected {check['list_status']}")
        if "package_type_substring" in check:
            pkg = (match.package_type or "").lower()
            src = (match.source_text or "").lower()
            needle = check["package_type_substring"].lower()
            if needle not in pkg and needle not in src:
                fail(f"{match.name} missing package info {needle!r}")
        if "source_text_substring" in check:
            src = (match.source_text or "").lower()
            if check["source_text_substring"].lower() not in src:
                fail(
                    f"{match.name} source_text missing "
                    f"{check['source_text_substring']!r}"
                )
        if check.get("has_alternative_group"):
            if not match.alternative_group_id:
                fail(f"{match.name} expected a non-null alternative_group_id")
        if "notes_substring" in check:
            notes = (match.notes or "").lower()
            if check["notes_substring"].lower() not in notes:
                fail(f"{match.name} notes missing {check['notes_substring']!r}")

    # Named alternatives must share one non-null group id (value is arbitrary).
    for group in expect.get("alternative_groups", []):
        members: list[ExtractedIngredient] = []
        missing = False
        for needle in group:
            match = _find_ingredient(result.ingredients, needle)
            if match is None:
                fail(
                    f"alternative group missing ingredient matching {needle!r}"
                )
                missing = True
                continue
            members.append(match)
        if missing or len(members) < 2:
            continue
        group_ids = {m.alternative_group_id for m in members}
        if None in group_ids or "" in group_ids:
            names = ", ".join(m.name for m in members)
            fail(
                f"alternatives [{names}] must each have a non-null "
                "alternative_group_id"
            )
        elif len(group_ids) != 1:
            detail = ", ".join(
                f"{m.name}={m.alternative_group_id!r}" for m in members
            )
            fail(
                "alternatives must share the same alternative_group_id "
                f"(got {detail})"
            )

    for line_sub in expect.get("display_line_substrings", []):
        all_lines = listed_lines + [
            format_ingredient_line(i).lower() for i in instruction_only
        ]
        if not any(line_sub.lower() in line for line in all_lines):
            fail(f"expected display line containing {line_sub!r}")

    for banned in expect.get("display_line_exclude_substrings", []):
        all_lines = listed_lines + [
            format_ingredient_line(i).lower() for i in instruction_only
        ]
        if any(banned.lower() in line for line in all_lines):
            fail(f"display must not contain {banned!r}")

    if "servings" in expect and result.servings != expect["servings"]:
        fail(f"servings expected {expect['servings']!r}, got {result.servings!r}")

    if "min_steps" in expect and len(result.steps) < int(expect["min_steps"]):
        fail(f"expected at least {expect['min_steps']} steps")

    if expect.get("steps_have_source_direction"):
        if not any(s.source_direction_text for s in result.steps):
            fail("expected at least one step with source_direction_text")

    if expect.get("has_alternative_group"):
        groups = {
            i.alternative_group_id for i in listed if i.alternative_group_id
        }
        if not groups:
            fail("expected an alternative_group_id on listed ingredients")
        for gid in groups:
            members = [i for i in listed if i.alternative_group_id == gid]
            if len(members) < 2:
                fail(f"alternative group {gid!r} needs >= 2 members")

    if expect.get("has_optional_ingredient"):
        if not any(i.optional for i in listed):
            fail("expected at least one optional listed ingredient")

    # Soft check: if the model labeled ignored chrome, accept any of these roles.
    # Absence alone is not a failure — silent omission of ads is fine.
    allowed_ignore_roles = {
        r.lower() for r in expect.get("ignored_boilerplate_roles_allowed", [])
    }
    if allowed_ignore_roles and result.ignored_boilerplate:
        for item in result.ignored_boilerplate:
            if item.role.lower() not in allowed_ignore_roles:
                fail(
                    f"ignored_boilerplate role {item.role!r} not in allowed "
                    f"{sorted(allowed_ignore_roles)}"
                )

    # Strict legacy check still used by cases that mock explicit role labels.
    for role in expect.get("ignored_boilerplate_roles", []):
        if not any(b.role == role for b in result.ignored_boilerplate):
            if not any(
                c.role == role and not c.kept_in_recipe for c in result.content_roles
            ):
                fail(f"expected ignored/boilerplate role {role!r}")

    # Page chrome must not contaminate recipe facts (ingredients/steps/notes).
    recipe_blobs: list[str] = []
    for item in result.ingredients:
        recipe_blobs.extend(
            [
                item.name or "",
                item.source_text or "",
                item.notes or "",
                item.quantity or "",
                item.unit or "",
            ]
        )
    for step in result.steps:
        recipe_blobs.extend([step.text or "", step.source_direction_text or ""])
    for note in result.creator_notes:
        recipe_blobs.append(note.text or "")
    recipe_text = "\n".join(recipe_blobs).lower()

    for banned in expect.get("recipe_content_exclude_substrings", []):
        if banned.lower() in recipe_text:
            fail(
                f"page-chrome text {banned!r} must not appear in ingredients, "
                "steps, or creator notes"
            )

    _ = case_id
    return failures


def run_semantic_assertions(
    case_id: str, result: ExtractionResult, expect: dict[str, Any]
) -> None:
    failures = collect_semantic_failures(case_id, result, expect)
    if failures:
        raise AssertionFailure(case_id, failures)
