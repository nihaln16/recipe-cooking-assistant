from __future__ import annotations

import base64
import io
import json
import logging
import re
import traceback
from pathlib import Path
from typing import Any, Protocol

from openai import OpenAI
from PIL import Image

from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.db import SourceBundle
from recipe_cooking_assistant.extraction_schema import (
    EXTRACTION_JSON_SCHEMA,
    SYSTEM_PROMPT,
)
from recipe_cooking_assistant.models import ExtractionResult, ExtractionUsage
from recipe_cooking_assistant.normalize import normalize_result

logger = logging.getLogger(__name__)

# gpt-4.1-mini list prices per 1M tokens (approximate; for logging only)
INPUT_PRICE_PER_M = 0.40
OUTPUT_PRICE_PER_M = 1.60
MAX_IMAGE_EDGE = 1600
SAFE_UI_API_ERROR = (
    "Extraction failed due to an API error. Please try again in a moment."
)
_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
_DATA_URL_RE = re.compile(r"data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+")


class ExtractionError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class ExtractionClient(Protocol):
    def extract(
        self, *, bundle: SourceBundle, settings: Settings
    ) -> tuple[ExtractionResult, ExtractionUsage]: ...


def estimate_cost_usd(
    input_tokens: int | None, output_tokens: int | None
) -> float | None:
    if input_tokens is None and output_tokens is None:
        return None
    cost = 0.0
    if input_tokens is not None:
        cost += input_tokens * INPUT_PRICE_PER_M / 1_000_000
    if output_tokens is not None:
        cost += output_tokens * OUTPUT_PRICE_PER_M / 1_000_000
    return round(cost, 6)


def sanitize_log_text(text: str, *, limit: int = 400) -> str:
    """Redact secrets and bulky payloads before logging."""
    message = text or ""
    message = _SECRET_RE.sub("[redacted-api-key]", message)
    message = _DATA_URL_RE.sub("[redacted-image-data]", message)
    message = " ".join(message.split())
    if len(message) > limit:
        return message[: limit - 3] + "..."
    return message


def sanitize_error_message(exc: BaseException, *, limit: int = 400) -> str:
    return sanitize_log_text(str(exc) or type(exc).__name__, limit=limit)


def extract_request_id(exc: BaseException) -> str | None:
    request_id = getattr(exc, "request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers is not None:
            header_id = headers.get("x-request-id") or headers.get("X-Request-ID")
            if header_id:
                return str(header_id)
    return None


def log_extraction_failure(exc: BaseException, *, bundle_id: str) -> None:
    stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error(
        "extraction_failed type=%s request_id=%s bundle_id=%s message=%s stack=%s",
        type(exc).__name__,
        extract_request_id(exc),
        bundle_id,
        sanitize_error_message(exc),
        sanitize_log_text(stack, limit=4000),
    )


def _resize_image_bytes(data: bytes, content_type: str) -> tuple[bytes, str]:
    """Downscale large images before sending to the API (cost guardrail)."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB") if img.mode not in ("RGB", "L") else img
            if img.mode == "L":
                img = img.convert("RGB")
            w, h = img.size
            longest = max(w, h)
            if longest > MAX_IMAGE_EDGE:
                scale = MAX_IMAGE_EDGE / longest
                img = img.resize(
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    Image.Resampling.LANCZOS,
                )
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=85, optimize=True)
            return out.getvalue(), "image/jpeg"
    except Exception:
        return data, content_type


def _data_url(path: Path, content_type: str) -> str:
    raw = path.read_bytes()
    raw, content_type = _resize_image_bytes(raw, content_type)
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return f"data:{content_type};base64,{b64}"


def build_user_content(bundle: SourceBundle) -> list[dict[str, Any]]:
    """Build Responses API multimodal content; images in upload order."""
    parts: list[dict[str, Any]] = []
    ordered = sorted(bundle.images, key=lambda img: img.sort_index)
    index_map = "\n".join(
        f"- image_index={img.sort_index}, image_id={img.id}, filename={img.filename}"
        for img in ordered
    ) or "- (no images)"

    text_block = (
        "Extract the recipe from the following source. "
        "Process screenshots strictly in the listed upload order.\n\n"
        f"Image index map (upload order):\n{index_map}\n\n"
    )
    if bundle.raw_text:
        text_block += f"Pasted text / caption:\n{bundle.raw_text}\n"
    else:
        text_block += "Pasted text / caption: (none)\n"

    parts.append({"type": "input_text", "text": text_block})

    for img in ordered:
        path = Path(img.stored_path)
        if not path.is_file():
            continue
        parts.append(
            {
                "type": "input_image",
                "image_url": _data_url(path, img.content_type),
                "detail": "high",
            }
        )
        parts.append(
            {
                "type": "input_text",
                "text": (
                    f"(End of screenshot image_index={img.sort_index}, "
                    f"image_id={img.id})"
                ),
            }
        )
    return parts


class OpenAIExtractionClient:
    """Extraction via OpenAI Responses API (`/v1/responses`) only."""

    def __init__(self, api_key: str, client: OpenAI | None = None) -> None:
        self._client = client or OpenAI(api_key=api_key)

    def extract(
        self, *, bundle: SourceBundle, settings: Settings
    ) -> tuple[ExtractionResult, ExtractionUsage]:
        try:
            response = self._client.responses.create(
                model=settings.openai_model,
                instructions=SYSTEM_PROMPT,
                input=[
                    {
                        "role": "user",
                        "content": build_user_content(bundle),
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "recipe_extraction",
                        "strict": True,
                        "schema": EXTRACTION_JSON_SCHEMA,
                    }
                },
                temperature=0,
                store=False,
            )
        except Exception as exc:
            log_extraction_failure(exc, bundle_id=bundle.id)
            raise ExtractionError(SAFE_UI_API_ERROR) from exc

        choice = getattr(response, "output_text", None) or ""
        if not choice.strip():
            raise ExtractionError("The model returned an empty extraction.")

        try:
            raw = json.loads(choice)
            result = normalize_result(
                ExtractionResult.model_validate(raw), bundle.images
            )
        except Exception as exc:
            log_extraction_failure(exc, bundle_id=bundle.id)
            raise ExtractionError(
                "Extraction returned data that could not be parsed. Please try again."
            ) from exc

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None) if usage else None
        output_tokens = getattr(usage, "output_tokens", None) if usage else None
        cost = estimate_cost_usd(input_tokens, output_tokens)
        response_id = getattr(response, "id", None)
        logger.info(
            "extraction_usage model=%s input_tokens=%s output_tokens=%s "
            "estimated_cost_usd=%s bundle_id=%s response_id=%s",
            settings.openai_model,
            input_tokens,
            output_tokens,
            cost,
            bundle.id,
            response_id,
        )
        return result, ExtractionUsage(
            model=settings.openai_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
        )


class StaticExtractionClient:
    """Test double that returns a fixed payload."""

    def __init__(self, result: ExtractionResult, usage: ExtractionUsage | None = None):
        self.result = result
        self.usage = usage or ExtractionUsage(
            model="mock",
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
        )

    def extract(
        self, *, bundle: SourceBundle, settings: Settings
    ) -> tuple[ExtractionResult, ExtractionUsage]:
        _ = settings
        return normalize_result(self.result.model_copy(deep=True), bundle.images), self.usage


def get_extraction_client(settings: Settings) -> ExtractionClient:
    if not settings.openai_api_key:
        raise ExtractionError(
            "OpenAI API key is not configured. Add OPENAI_API_KEY to your "
            "local .env (never paste it into chat), then try again."
        )
    return OpenAIExtractionClient(settings.openai_api_key)


# Re-export for tests / typing clarity
__all__ = [
    "ExtractionClient",
    "ExtractionError",
    "OpenAIExtractionClient",
    "SAFE_UI_API_ERROR",
    "StaticExtractionClient",
    "build_user_content",
    "estimate_cost_usd",
    "extract_request_id",
    "get_extraction_client",
    "log_extraction_failure",
    "normalize_result",
    "sanitize_error_message",
    "sanitize_log_text",
]
