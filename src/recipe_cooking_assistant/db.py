from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.models import (
    ExtractionResult,
    ExtractionUsage,
    StoredExtraction,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class SourceImage:
    id: str
    bundle_id: str
    session_id: str
    filename: str
    stored_path: str
    content_type: str
    size_bytes: int
    sort_index: int
    created_at: str


@dataclass
class SourceBundle:
    id: str
    session_id: str
    raw_text: str | None
    created_at: str
    expires_at: str
    images: list[SourceImage]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_bundles (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    raw_text TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS source_images (
                    id TEXT PRIMARY KEY,
                    bundle_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sort_index INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (bundle_id) REFERENCES source_bundles(id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS extracted_recipes (
                    id TEXT PRIMARY KEY,
                    bundle_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    model TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    estimated_cost_usd REAL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (bundle_id) REFERENCES source_bundles(id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_bundles_session
                    ON source_bundles(session_id);
                CREATE INDEX IF NOT EXISTS idx_images_bundle
                    ON source_images(bundle_id);
                CREATE INDEX IF NOT EXISTS idx_recipes_session
                    ON extracted_recipes(session_id);
                CREATE INDEX IF NOT EXISTS idx_recipes_bundle
                    ON extracted_recipes(bundle_id);
                """
            )
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(source_images)").fetchall()
            }
            if "sort_index" not in cols:
                conn.execute(
                    "ALTER TABLE source_images ADD COLUMN sort_index INTEGER NOT NULL DEFAULT 0"
                )

    def create_bundle(
        self,
        *,
        session_id: str,
        raw_text: str | None,
        settings: Settings,
        images: list[tuple[str, str, str, str, int, int]],
        bundle_id: str | None = None,
    ) -> SourceBundle:
        """images: (id, filename, stored_path, content_type, size_bytes, sort_index)."""
        bundle_id = bundle_id or str(uuid4())
        now = utcnow()
        expires = now + timedelta(hours=settings.session_ttl_hours)
        created_at = isoformat(now)
        expires_at = isoformat(expires)

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO source_bundles (id, session_id, raw_text, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (bundle_id, session_id, raw_text, created_at, expires_at),
            )
            image_rows: list[SourceImage] = []
            for (
                image_id,
                filename,
                stored_path,
                content_type,
                size_bytes,
                sort_index,
            ) in images:
                conn.execute(
                    """
                    INSERT INTO source_images (
                        id, bundle_id, session_id, filename, stored_path,
                        content_type, size_bytes, sort_index, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        image_id,
                        bundle_id,
                        session_id,
                        filename,
                        stored_path,
                        content_type,
                        size_bytes,
                        sort_index,
                        created_at,
                    ),
                )
                image_rows.append(
                    SourceImage(
                        id=image_id,
                        bundle_id=bundle_id,
                        session_id=session_id,
                        filename=filename,
                        stored_path=stored_path,
                        content_type=content_type,
                        size_bytes=size_bytes,
                        sort_index=sort_index,
                        created_at=created_at,
                    )
                )

        return SourceBundle(
            id=bundle_id,
            session_id=session_id,
            raw_text=raw_text,
            created_at=created_at,
            expires_at=expires_at,
            images=image_rows,
        )

    def get_bundle_for_session(
        self, bundle_id: str, session_id: str
    ) -> SourceBundle | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, session_id, raw_text, created_at, expires_at
                FROM source_bundles
                WHERE id = ? AND session_id = ?
                """,
                (bundle_id, session_id),
            ).fetchone()
            if row is None:
                return None

            expires_at = parse_iso(row["expires_at"])
            if expires_at <= utcnow():
                return None

            image_rows = conn.execute(
                """
                SELECT id, bundle_id, session_id, filename, stored_path,
                       content_type, size_bytes, sort_index, created_at
                FROM source_images
                WHERE bundle_id = ? AND session_id = ?
                ORDER BY sort_index ASC, created_at ASC
                """,
                (bundle_id, session_id),
            ).fetchall()

        images = [
            SourceImage(
                id=img["id"],
                bundle_id=img["bundle_id"],
                session_id=img["session_id"],
                filename=img["filename"],
                stored_path=img["stored_path"],
                content_type=img["content_type"],
                size_bytes=img["size_bytes"],
                sort_index=img["sort_index"],
                created_at=img["created_at"],
            )
            for img in image_rows
        ]
        return SourceBundle(
            id=row["id"],
            session_id=row["session_id"],
            raw_text=row["raw_text"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            images=images,
        )

    def save_extraction(
        self,
        *,
        bundle: SourceBundle,
        result: ExtractionResult,
        usage: ExtractionUsage,
        settings: Settings,
        extraction_id: str | None = None,
    ) -> StoredExtraction:
        extraction_id = extraction_id or str(uuid4())
        now = utcnow()
        created_at = isoformat(now)
        expires_at = isoformat(now + timedelta(hours=settings.session_ttl_hours))
        payload = result.model_dump_json()

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO extracted_recipes (
                    id, bundle_id, session_id, payload_json, model,
                    input_tokens, output_tokens, estimated_cost_usd,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    extraction_id,
                    bundle.id,
                    bundle.session_id,
                    payload,
                    usage.model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.estimated_cost_usd,
                    created_at,
                    expires_at,
                ),
            )

        return StoredExtraction(
            id=extraction_id,
            bundle_id=bundle.id,
            session_id=bundle.session_id,
            result=result,
            usage=usage,
            created_at=created_at,
            expires_at=expires_at,
        )

    def get_extraction_for_session(
        self, extraction_id: str, session_id: str
    ) -> StoredExtraction | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, bundle_id, session_id, payload_json, model,
                       input_tokens, output_tokens, estimated_cost_usd,
                       created_at, expires_at
                FROM extracted_recipes
                WHERE id = ? AND session_id = ?
                """,
                (extraction_id, session_id),
            ).fetchone()
        if row is None:
            return None
        if parse_iso(row["expires_at"]) <= utcnow():
            return None

        result = ExtractionResult.model_validate(json.loads(row["payload_json"]))
        usage = ExtractionUsage(
            model=row["model"] or "unknown",
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            estimated_cost_usd=row["estimated_cost_usd"],
        )
        return StoredExtraction(
            id=row["id"],
            bundle_id=row["bundle_id"],
            session_id=row["session_id"],
            result=result,
            usage=usage,
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def purge_expired(self) -> list[str]:
        """Delete expired bundles; return stored_paths for file cleanup."""
        now = isoformat(utcnow())
        with self.connect() as conn:
            paths = [
                row["stored_path"]
                for row in conn.execute(
                    """
                    SELECT si.stored_path
                    FROM source_images si
                    JOIN source_bundles sb ON sb.id = si.bundle_id
                    WHERE sb.expires_at <= ?
                    """,
                    (now,),
                ).fetchall()
            ]
            conn.execute(
                "DELETE FROM extracted_recipes WHERE expires_at <= ?", (now,)
            )
            conn.execute("DELETE FROM source_bundles WHERE expires_at <= ?", (now,))
        return paths
