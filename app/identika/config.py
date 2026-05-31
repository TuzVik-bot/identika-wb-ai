from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env",), extra="ignore")

    identika_provider: str = "mock"
    identika_host: str = "127.0.0.1"
    identika_port: int = 8787
    identika_db_path: Path = Path("./data/identika.sqlite")
    identika_assets_dir: Path = Path("./assets")

    wb_tool_base_url: str = "http://127.0.0.1:8765"
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
    def enable_ai_images(self) -> bool:
        if self.identika_enable_ai_images is not None:
            return self.identika_enable_ai_images
        return self.provider == "openrouter"

    @property
    def public_base_path(self) -> str:
        value = self.identika_public_base_path.strip()
        if not value or value == "/":
            return ""
        return "/" + value.strip("/")


settings = Settings()
