from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from identika.config import settings
from identika.models import CreateJobRequest, ProductContext, ProductImage
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
    assert any("OpenRouter" in item for item in result.info)


def test_openrouter_enforces_white_background_and_kit_contract(monkeypatch) -> None:
    settings.identika_provider = "openrouter"
    settings.openrouter_api_key = "test-key"
    settings.identika_enable_ai_images = False

    plan = _text_plan()
    plan["slides"][5]["bullets"] = ["Лишний текст"]
    plan["slides"][9]["title"] = "Неверный заголовок"
    plan["slides"][9]["subtitle"] = "Неверный подзаголовок"
    plan["slides"][9]["bullets"] = ["Товар", "Кабель", "Упаковка", "Инструкция", "Гарантия"]

    fake_client = FakeAsyncClient()
    fake_client.response_json = {
        "choices": [{"message": {"content": json.dumps(plan, ensure_ascii=False)}}]
    }
    monkeypatch.setattr("identika.providers.openrouter.httpx.AsyncClient", lambda *a, **k: fake_client)

    result = asyncio.run(
        OpenRouterProvider().generate(CreateJobRequest(product=ProductContext(title="Тест")))
    )

    slide6 = result.slides[5]
    slide10 = result.slides[9]
    assert slide6.bullets == []
    assert slide10.title == "Комплект поставки"
    assert slide10.subtitle == "Инфографика состава комплекта"
    assert slide10.bullets == ["Товар", "Кабель", "Упаковка", "Инструкция"]
    title_block = next(block for block in slide10.text_blocks if block.kind == "title")
    subtitle_block = next(block for block in slide10.text_blocks if block.kind == "subtitle")
    assert title_block.text == slide10.title
    assert subtitle_block.text == slide10.subtitle


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


def test_openrouter_image_generation_skips_white_and_description_with_sources(
    tmp_path, monkeypatch
) -> None:
    settings.identika_provider = "openrouter"
    settings.openrouter_api_key = "test-key"
    settings.identika_enable_ai_images = True

    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    job = storage.create_job(CreateJobRequest(product=ProductContext(title="Тест")).model_dump(mode="json"))
    source_id = storage.add_asset(
        job.id,
        "product.png",
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
            "530000000a49444154789c6260000000020001e221bc330000000049454e44ae426082"
        ),
        "image/png",
    )

    calls: list[str] = []

    class CountingClient(FakeAsyncClient):
        async def post(self, url: str, **kwargs):
            payload = kwargs.get("json") or {}
            messages = payload.get("messages") or []
            if messages:
                content = messages[0].get("content") or []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        calls.append(str(part.get("text", "")))
            return await super().post(url, **kwargs)

    fake_client = CountingClient()
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
        OpenRouterProvider().generate(
            CreateJobRequest(
                product=ProductContext(
                    title="Тест",
                    images=[ProductImage(asset_id=source_id, role="source")],
                )
            )
        )
    )
    base_result.product.images[0].asset_id = source_id
    updated = asyncio.run(
        generate_slide_images(
            job.id,
            CreateJobRequest(
                product=ProductContext(
                    title="Тест",
                    images=[ProductImage(asset_id=source_id, role="source")],
                )
            ),
            base_result,
            storage,
        )
    )
    assert len(calls) == 1
    assert updated.slides[0].background_asset_id
    assert not updated.slides[1].background_asset_id
    assert not updated.slides[5].background_asset_id
    assert "NO text" in calls[0]
