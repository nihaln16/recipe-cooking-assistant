"""JSON Schema for OpenAI structured extraction (strict mode)."""

from __future__ import annotations

_EVIDENCE = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "quote": {"type": ["string", "null"]},
        "image_id": {"type": ["string", "null"]},
        "image_index": {"type": ["integer", "null"]},
    },
    "required": ["quote", "image_id", "image_index"],
}

_INGREDIENT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "quantity": {
            "type": ["string", "null"],
            "description": (
                "Numeric or measurable amount only (e.g. '1', '1/2', '8'). "
                "Do NOT put qualifiers here. Do NOT invent amounts."
            ),
        },
        "unit": {
            "type": ["string", "null"],
            "description": "Unit of measure (cup, tsp, ounces, g). Not a qualifier.",
        },
        "notes": {
            "type": ["string", "null"],
            "description": (
                "Qualifiers and prep notes: to taste, as needed, for garnish, "
                "divided, rinsed and drained, minced, with their juice, etc."
            ),
        },
        "source_text": {
            "type": ["string", "null"],
            "description": (
                "Exact ingredient line/phrase from the source when available. "
                "Preserve wording, package info, and order."
            ),
        },
        "list_status": {
            "type": "string",
            "enum": ["listed", "instruction_only"],
            "description": (
                "listed = appears in the explicit ingredient list. "
                "instruction_only = mentioned in steps/directions but absent "
                "from the ingredient list (do not invent a quantity)."
            ),
        },
        "optional": {
            "type": "boolean",
            "description": "True for optional items and garnish choices.",
        },
        "alternative_group_id": {
            "type": ["string", "null"],
            "description": (
                "Shared id when items are alternatives (OR), e.g. turkey OR soy. "
                "Do not treat alternatives as all required."
            ),
        },
        "package_count": {
            "type": ["string", "null"],
            "description": "Package count as written, e.g. '1' or 'One'.",
        },
        "package_size": {
            "type": ["string", "null"],
            "description": "Package size as written, e.g. '14 1/2-ounce'.",
        },
        "package_type": {
            "type": ["string", "null"],
            "description": "Package type, e.g. 'can'.",
        },
        "provenance": {
            "type": "string",
            "enum": ["source", "needs_review"],
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "uncertain"],
        },
        "evidence": _EVIDENCE,
    },
    "required": [
        "id",
        "name",
        "quantity",
        "unit",
        "notes",
        "source_text",
        "list_status",
        "optional",
        "alternative_group_id",
        "package_count",
        "package_size",
        "package_type",
        "provenance",
        "confidence",
        "evidence",
    ],
}

_STEP = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
        "text": {"type": "string"},
        "provenance": {
            "type": "string",
            "enum": ["source", "needs_review"],
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "uncertain"],
        },
        "evidence": _EVIDENCE,
        "related_ingredient_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "source_direction_text": {
            "type": ["string", "null"],
            "description": (
                "When splitting a long direction into atomic steps, copy the "
                "original full direction text here for evidence linkage."
            ),
        },
    },
    "required": [
        "id",
        "text",
        "provenance",
        "confidence",
        "evidence",
        "related_ingredient_ids",
        "source_direction_text",
    ],
}

_FINDING = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
        "type": {
            "type": "string",
            "enum": [
                "uncertain_ordering",
                "contradiction",
                "uncertain_extraction",
                "instruction_only_ingredient",
                "missing_quantity",
                "other",
            ],
        },
        "message": {"type": "string"},
        "related_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "evidence": _EVIDENCE,
    },
    "required": ["id", "type", "message", "related_ids", "evidence"],
}

EXTRACTION_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "insufficient_source": {
            "type": "boolean",
            "description": (
                "True when there is no readable recipe content "
                "(e.g. only a finished-dish photo). Do not invent a recipe."
            ),
        },
        "insufficient_reason": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "servings": {
            "type": ["string", "null"],
            "description": "Numeric servings when known (e.g. '4'), not '4 servings'.",
        },
        "ingredients": {"type": "array", "items": _INGREDIENT},
        "steps": {"type": "array", "items": _STEP},
        "creator_notes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "provenance": {
                        "type": "string",
                        "enum": ["source", "needs_review"],
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "uncertain"],
                    },
                    "evidence": _EVIDENCE,
                },
                "required": [
                    "id",
                    "text",
                    "provenance",
                    "confidence",
                    "evidence",
                ],
            },
        },
        "content_roles": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "role": {
                        "type": "string",
                        "enum": [
                            "ingredient",
                            "instruction",
                            "creator_note",
                            "step_photo_caption",
                            "credit",
                            "anecdote",
                            "ad",
                            "boilerplate",
                            "dish_photo",
                            "other",
                        ],
                    },
                    "summary": {"type": "string"},
                    "image_index": {"type": ["integer", "null"]},
                    "kept_in_recipe": {"type": "boolean"},
                },
                "required": ["role", "summary", "image_index", "kept_in_recipe"],
            },
        },
        "review_flags": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "uncertain_ordering",
                            "contradiction",
                            "uncertain_extraction",
                            "instruction_only_ingredient",
                            "missing_quantity",
                            "other",
                        ],
                    },
                    "message": {"type": "string"},
                    "related_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["type", "message", "related_ids"],
            },
        },
        "findings": {"type": "array", "items": _FINDING},
        "ignored_boilerplate": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "summary": {"type": "string"},
                    "role": {
                        "type": "string",
                        "enum": [
                            "ingredient",
                            "instruction",
                            "creator_note",
                            "step_photo_caption",
                            "credit",
                            "anecdote",
                            "ad",
                            "boilerplate",
                            "dish_photo",
                            "other",
                        ],
                    },
                },
                "required": ["summary", "role"],
            },
        },
    },
    "required": [
        "insufficient_source",
        "insufficient_reason",
        "title",
        "servings",
        "ingredients",
        "steps",
        "creator_notes",
        "content_roles",
        "review_flags",
        "findings",
        "ignored_boilerplate",
    ],
}

SYSTEM_PROMPT = """You extract ONE structured cooking recipe from user-provided text and/or screenshots.

Scope:
- One recipe only. Do not scrape the web or invent content from video.
- Screenshots are provided in UPLOAD ORDER (image_index 0, 1, 2, ...). Preserve that order when merging steps. Deduplicate overlapping screenshot text.
- Finished-dish photos may be visual context only. Never infer an exact recipe from a dish photo alone.
- If there is no readable recipe content (ingredients and/or steps), set insufficient_source=true, explain in insufficient_reason, and leave recipe arrays empty. Do not invent ingredients or steps.

Messy sources:
- Classify notable regions in content_roles.
- Ignore ads, site chrome, SEO fluff, and pure credits/boilerplate (list briefly in ignored_boilerplate).
- Preserve meaningful creator notes in creator_notes—not as fake ingredients.
- Step photo captions that are instructions belong in steps; decorative captions do not.

Ingredients:
- Prefer source_text = the exact source ingredient phrase when available (preserves package wording and natural order).
- Put measurable amounts in quantity/unit. Never invent amounts (e.g. leave olive oil quantity null if none given).
- Qualifiers "to taste", "as needed", "for garnish", "divided" go in notes with quantity/unit null.
- Preserve package_count, package_size, package_type for canned/packaged items (e.g. One 14 1/2-ounce can...).
- Preserve preparation notes (rinsed and drained, with their juice, minced, etc.) in notes.
- Alternatives joined by "or" share one alternative_group_id; they are choices, not both required.
- Optional garnishes/choices: set optional=true; do not collapse a garnish group into one required ingredient.
- list_status=listed for items on the explicit ingredient list.
- list_status=instruction_only for items mentioned only in instructions/directions and absent from the ingredient list. Do not invent their quantities. Add a finding of type instruction_only_ingredient with message like "Referenced in instructions but absent from ingredient list."
- Do not present instruction_only items as ordinary listed ingredients.
- Avoid awkward reordering in name when source_text can carry the natural phrasing (prefer "5 garlic cloves" style via source_text, not "Garlic cloves" with quantity 5 alone if that loses clarity).

Servings:
- servings should be the number only when known (e.g. "4"), not "4 servings".

Steps:
- You may split long directions into smaller atomic steps for cooking.
- When you split, set source_direction_text to the original full direction and keep evidence linking to the source.
- If screenshot order is unclear or contradictory, add uncertain_ordering / contradiction findings—do not silently resolve.

Findings / review_flags:
- Use findings for instruction_only_ingredient, missing_quantity (when useful), contradiction, uncertain_ordering, uncertain_extraction.
- Mirror important flags in review_flags as well when relevant.
- When uncertain, set confidence=uncertain and provenance=needs_review — do not assert uncertain fields as settled source facts.

IDs: use short stable ids like ing_1, step_1, note_1, find_1, alt_1.
"""
