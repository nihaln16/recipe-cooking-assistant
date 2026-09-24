from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Recipe Cooking Assistant"
    session_secret: str = Field(
        default="dev-only-change-me",
        validation_alias="SESSION_SECRET",
    )
    data_dir: Path = Field(default=Path("."), validation_alias="DATA_DIR")
    session_ttl_hours: int = Field(default=24, validation_alias="SESSION_TTL_HOURS")
    max_images: int = Field(default=6, validation_alias="MAX_IMAGES")
    max_upload_bytes: int = Field(
        default=4 * 1024 * 1024, validation_alias="MAX_UPLOAD_BYTES"
    )
    allowed_image_types: frozenset[str] = frozenset(
        {"image/jpeg", "image/png", "image/webp"}
    )
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", validation_alias="OPENAI_MODEL")
    import_limit_per_client: int = Field(
        default=8, validation_alias="IMPORT_LIMIT_PER_CLIENT"
    )
    import_limit_per_process: int = Field(
        default=30, validation_alias="IMPORT_LIMIT_PER_PROCESS"
    )
    import_limit_window_seconds: int = Field(
        default=3600, validation_alias="IMPORT_LIMIT_WINDOW_SECONDS"
    )
    guidance_limit_per_session: int = Field(
        default=20, validation_alias="GUIDANCE_LIMIT_PER_SESSION"
    )
    guidance_limit_per_process: int = Field(
        default=60, validation_alias="GUIDANCE_LIMIT_PER_PROCESS"
    )
    guidance_limit_window_seconds: int = Field(
        default=3600, validation_alias="GUIDANCE_LIMIT_WINDOW_SECONDS"
    )

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "app.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
