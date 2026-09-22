from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


Confidence = Literal["high", "uncertain"]
Provenance = Literal["source", "needs_review"]
ListStatus = Literal["listed", "instruction_only"]
ReviewFlagType = Literal[
    "uncertain_ordering",
    "contradiction",
    "uncertain_extraction",
    "instruction_only_ingredient",
    "missing_quantity",
    "other",
]
FindingType = ReviewFlagType
ContentRole = Literal[
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
]


class SourceEvidence(BaseModel):
    quote: str | None = None
    image_id: str | None = None
    image_index: int | None = Field(
        default=None,
        description="0-based index in upload order",
    )


class ExtractedIngredient(BaseModel):
    id: str
    name: str
    quantity: str | None = None
    unit: str | None = None
    notes: str | None = None
    source_text: str | None = Field(
        default=None,
        description="Exact ingredient phrase as written in the source, when available.",
    )
    list_status: ListStatus = "listed"
    optional: bool = False
    alternative_group_id: str | None = Field(
        default=None,
        description="Shared id for mutually exclusive alternatives (OR), not all required.",
    )
    package_count: str | None = None
    package_size: str | None = None
    package_type: str | None = None
    provenance: Provenance = "source"
    confidence: Confidence = "high"
    evidence: SourceEvidence | None = None


class ExtractedStep(BaseModel):
    id: str
    text: str
    provenance: Provenance = "source"
    confidence: Confidence = "high"
    evidence: SourceEvidence | None = None
    related_ingredient_ids: list[str] = Field(default_factory=list)
    source_direction_text: str | None = Field(
        default=None,
        description="Original source direction this atomic step was split from.",
    )


class CreatorNote(BaseModel):
    id: str
    text: str
    provenance: Provenance = "source"
    confidence: Confidence = "high"
    evidence: SourceEvidence | None = None


class ContentRoleItem(BaseModel):
    role: ContentRole
    summary: str
    image_index: int | None = None
    kept_in_recipe: bool = False


class ReviewFlag(BaseModel):
    type: ReviewFlagType
    message: str
    related_ids: list[str] = Field(default_factory=list)


class ExtractionFinding(BaseModel):
    id: str
    type: FindingType
    message: str
    related_ids: list[str] = Field(default_factory=list)
    evidence: SourceEvidence | None = None


class IgnoredBoilerplate(BaseModel):
    summary: str
    role: ContentRole = "boilerplate"


class ExtractionResult(BaseModel):
    insufficient_source: bool = False
    insufficient_reason: str | None = None
    title: str | None = None
    servings: str | None = None
    ingredients: list[ExtractedIngredient] = Field(default_factory=list)
    steps: list[ExtractedStep] = Field(default_factory=list)
    creator_notes: list[CreatorNote] = Field(default_factory=list)
    content_roles: list[ContentRoleItem] = Field(default_factory=list)
    review_flags: list[ReviewFlag] = Field(default_factory=list)
    findings: list[ExtractionFinding] = Field(default_factory=list)
    ignored_boilerplate: list[IgnoredBoilerplate] = Field(default_factory=list)

    def listed_ingredients(self) -> list[ExtractedIngredient]:
        return [i for i in self.ingredients if i.list_status == "listed"]

    def instruction_only_ingredients(self) -> list[ExtractedIngredient]:
        return [i for i in self.ingredients if i.list_status == "instruction_only"]


class ExtractionUsage(BaseModel):
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None


class StoredExtraction(BaseModel):
    id: str
    bundle_id: str
    session_id: str
    result: ExtractionResult
    usage: ExtractionUsage
    created_at: str
    expires_at: str
