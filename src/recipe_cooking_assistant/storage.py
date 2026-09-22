from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from recipe_cooking_assistant.config import Settings

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")

EXTENSION_FOR_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class UploadError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def ensure_upload_root(settings: Settings) -> Path:
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    return settings.upload_dir


def sanitize_filename(name: str) -> str:
    base = Path(name or "image").name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "image"
    return cleaned[:120]


async def save_uploads(
    *,
    files: list[UploadFile],
    session_id: str,
    bundle_id: str,
    settings: Settings,
) -> list[tuple[str, str, str, str, int, int]]:
    """Validate and save uploads in order.

    Returns (id, filename, stored_path, content_type, size, sort_index).
    """
    non_empty = [f for f in files if f.filename]
    if len(non_empty) > settings.max_images:
        raise UploadError(
            f"You can upload at most {settings.max_images} images per recipe."
        )

    ensure_upload_root(settings)
    dest_dir = settings.upload_dir / session_id / bundle_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved: list[tuple[str, str, str, str, int, int]] = []
    try:
        for sort_index, upload in enumerate(non_empty):
            content_type = (upload.content_type or "").lower()
            if content_type not in settings.allowed_image_types:
                raise UploadError(
                    "Only JPEG, PNG, and WebP images are allowed."
                )

            data = await upload.read()
            size = len(data)
            if size == 0:
                raise UploadError("One of the selected files was empty.")
            if size > settings.max_upload_bytes:
                limit_mb = settings.max_upload_bytes / (1024 * 1024)
                raise UploadError(
                    f"Each image must be {limit_mb:.0f} MB or smaller."
                )

            image_id = str(uuid4())
            filename = sanitize_filename(upload.filename or "image")
            ext = EXTENSION_FOR_TYPE[content_type]
            if not filename.lower().endswith(ext):
                filename = f"{Path(filename).stem}{ext}"

            stored_path = dest_dir / f"{image_id}{ext}"
            stored_path.write_bytes(data)
            saved.append(
                (
                    image_id,
                    filename,
                    str(stored_path),
                    content_type,
                    size,
                    sort_index,
                )
            )
    except Exception:
        for _, _, path, _, _, _ in saved:
            Path(path).unlink(missing_ok=True)
        if dest_dir.exists() and not any(dest_dir.iterdir()):
            dest_dir.rmdir()
        raise

    return saved


def delete_paths(paths: list[str]) -> None:
    for path_str in paths:
        path = Path(path_str)
        path.unlink(missing_ok=True)
        parent = path.parent
        if parent.exists() and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            grand = parent.parent
            if grand.exists() and grand.is_dir() and not any(grand.iterdir()):
                grand.rmdir()
