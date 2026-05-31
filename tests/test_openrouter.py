from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from identika.config import settings
from identika.models import CreateJobRequest, ProductContext
from identika.providers.image_gen import generate_slide_images
from identika.providers.openrouter import OpenRouterProvider, get_provider
from identika.providers.mock import MockProvider
from identika.storage import Storage


def test_openrouter_without_api_key_falls_back_to_mock() -> None:
    settings.identika_provider = "openrouter"
    settings.openrouter_api_key = ""
    assert settings.effective_provider == "mock"
    assert isinstance(get_provider(), MockProvider)
    result = asyncio.run(
        OpenRouterProvider().generate(CreateJobRequest(product=ProductContext(title="Тест")))
    )
    assert result.provider == "mock"
    assert any("OPENROUTER_API_KEY is empty" in warning for warning in result.warnings)


def _png_data_uri() -> str:
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "530000000a49444154789c6260000000020001e221bc330000000049454e44ae426082"
    )
    import base64

    return f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"


def _text_plan() -> dict[str, Any]:
    slides = []
    for idx in range(1, 11):
        slides.append(
            {
                "index": idx,
                "title": f"Заголовок {idx}",
                "subtitle": f"Подзаголовок {idx}",
                "bullets": ["Плюс 1", "Плюс 2"],
            }
        )
    return {"slides": slides, "rich_blocks": [], "warnings": []}


class FakeAsyncClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.response_json: dict[str, Any] = {}

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> AsyncMock:
        response = AsyncMock()
        response.raise_for_status = lambda: None
        response.json = lambda: self.response_json
        return response


def test_openrouter_text_plan_is_applied(tmp_path, monkeypatch) -> None:
    settings.identika_provider = "openrouter"
    settings.openrouter_api_key = "test-key"
    settings.identika_enable_ai_images = False

    fake_client = FakeAsyncClient()
    fake_client.response_json = {
        "choices": [{"message": {"content": json.dumps(_text_plan(), ensure_ascii=False)}}]
    }
    monkeypatch.setattr("identika.providers.openrouter.httpx.AsyncClient", lambda *a, **k: fake_client)

    result = asyncio.run(
        OpenRouterProvider().generate(CreateJobRequest(product=ProductContext(title="Тест")))
    )
    assert result.slides[0].title == "Заголовок 1"
    assert any("OpenRouter" in warning for warning in result.warnings)


def test_openrouter_image_generation_stores_background_assets(tmp_path, monkeypatch) -> None:
    settings.identika_provider = "openrouter"
    settings.openrouter_api_key = "test-key"
    settings.identika_enable_ai_images = True

    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    job = storage.create_job(CreateJobRequest(product=ProductContext(title="Тест")).model_dump(mode="json"))

    fake_client = FakeAsyncClient()
    fake_client.response_json = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "images": [{"image_url": {"url": _png_data_uri()}}],
                }
            }
        ]
    }
    monkeypatch.setattr("identika.providers.image_gen.httpx.AsyncClient", lambda *a, **k: fake_client)

    base_result = asyncio.run(
        OpenRouterProvider().generate(CreateJobRequest(product=ProductContext(title="Тест")))
    )
    updated = asyncio.run(
        generate_slide_images(
            job.id,
            CreateJobRequest(product=ProductContext(title="Тест")),
            base_result,
            storage,
        )
    )
    assert updated.slides[0].background_asset_id
    path, media_type = storage.get_asset(updated.slides[0].background_asset_id)
    assert media_type == "image/png"
    assert path.read_bytes().startswith(b"\x89PNG")
