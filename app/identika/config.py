from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from identika.storage import Storage


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env",), extra="ignore")

    identika_provider: str = "mock"
    identika_host: str = "127.0.0.1"
    identika_port: int = 8787
    identika_db_path: Path = Path("./data/identika.sqlite")
    identika_assets_dir: Path = Path("./assets")

    wb_tool_base_url: str = "http://127.0.0.1:8765"
    wb_tool_public_url: str = ""
    identika_public_base_path: str = ""

    openrouter_api_key: str = ""
    openrouter_image_model: str = "google/gemini-3.1-flash-image-preview"
    openrouter_text_model: str = "google/gemini-3.1-flash-lite-preview"
    identika_enable_ai_images: bool | None = None

    identika_api_key: str = ""
    identika_ui_password: str = ""

    @property
    def provider(self) -> str:
        return self.identika_provider.strip().lower() or "mock"

    @property
    def effective_provider(self) -> str:
        """Provider used at runtime (openrouter without key falls back to mock)."""
        if self.provider == "openrouter" and not self.openrouter_api_key.strip():
            return "mock"
        return self.provider

    @property
    def enable_ai_images(self) -> bool:
        if self.identika_enable_ai_images is not None:
            return self.identika_enable_ai_images
        return self.effective_provider == "openrouter"

    @property
    def public_base_path(self) -> str:
        value = self.identika_public_base_path.strip()
        if not value or value == "/":
            return ""
        return "/" + value.strip("/")

    @property
    def static_url_prefix(self) -> str:
        return f"{self.public_base_path}/static"

    def is_static_path(self, path: str) -> bool:
        if path.startswith("/static/") or path == "/static":
            return True
        prefix = self.static_url_prefix
        return bool(prefix) and (path.startswith(f"{prefix}/") or path == prefix)

    @property
    def wb_tool_display_url(self) -> str:
        public = self.wb_tool_public_url.strip().rstrip("/")
        if public:
            return public
        internal = self.wb_tool_base_url.strip().rstrip("/")
        if not internal:
            return ""
        lowered = internal.lower()
        if "127.0.0.1" in lowered or "localhost" in lowered:
            return ""
        return internal


settings = Settings()


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class EffectiveSettings:
    provider: str
    openrouter_api_key: str
    openrouter_text_model: str
    openrouter_image_model: str
    enable_ai_images: bool

    @property
    def effective_provider(self) -> str:
        if self.provider == "openrouter" and not self.openrouter_api_key.strip():
            return "mock"
        return self.provider

    @classmethod
    def resolve(cls, storage: Storage | None = None) -> EffectiveSettings:
        db = storage.get_settings() if storage else {}

        def pick_str(key: str, default: str) -> str:
            if key in db:
                return db[key].strip()
            return default

        provider = pick_str("provider", settings.provider)
        openrouter_api_key = pick_str("openrouter_api_key", settings.openrouter_api_key)
        openrouter_text_model = pick_str("openrouter_text_model", settings.openrouter_text_model)
        openrouter_image_model = pick_str("openrouter_image_model", settings.openrouter_image_model)

        if "enable_ai_images" in db:
            enable_ai_images = _parse_bool(db["enable_ai_images"])
        elif settings.identika_enable_ai_images is not None:
            enable_ai_images = settings.identika_enable_ai_images
        else:
            enable_ai_images = (provider or settings.provider) == "openrouter"

        return cls(
            provider=provider or "mock",
            openrouter_api_key=openrouter_api_key,
            openrouter_text_model=openrouter_text_model,
            openrouter_image_model=openrouter_image_model,
            enable_ai_images=enable_ai_images,
        )


def mask_api_key(value: str) -> str:
    clean = value.strip()
    if not clean:
        return ""
    if len(clean) <= 4:
        return "••••"
    return f"••••{clean[-4:]}"
