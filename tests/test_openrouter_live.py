from __future__ import annotations

import asyncio
import io
import os
import zipfile

import pytest
from PIL import Image

from identika.models import CreateJobRequest, ProductContext
from identika.services.jobs import JobService
from identika.storage import Storage


pytestmark = pytest.mark.live_openrouter


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _openrouter_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def _skip_without_live_flag() -> str:
    api_key = _openrouter_key()
    if not _truthy(os.environ.get("RUN_OPENROUTER_E2E")):
        pytest.skip("set RUN_OPENROUTER_E2E=1 to run live OpenRouter tests")
    if not api_key:
        pytest.skip("set OPENROUTER_API_KEY to run live OpenRouter tests")
    return api_key


def _configure_openrouter(storage: Storage, api_key: str, *, images: bool) -> None:
    storage.set_settings(
        {
            "provider": "openrouter",
            "openrouter_api_key": api_key,
            "openrouter_text_model": os.environ.get(
                "OPENROUTER_TEXT_MODEL",
                "google/gemini-3.1-flash-lite-preview",
            ),
            "openrouter_image_model": os.environ.get(
                "OPENROUTER_IMAGE_MODEL",
                "google/gemini-3.1-flash-image-preview",
            ),
            "enable_ai_images": "true" if images else "false",
        }
    )


def _product() -> ProductContext:
    return ProductContext(
        store_slug="live-openrouter",
        sku_id=9001200,
        nm_id=1440900,
        vendor_code="LIVE-OPENROUTER-SMOKE",
        title="USB-C кабель 100W в нейлоновой оплётке",
        brand="Identika Test",
        subject_name="Кабели",
        description="Тестовый товар для live-smoke генерации карточки Wildberries.",
        characteristics={
            "Мощность": "100 Вт",
            "Длина": "1 м",
            "Материал": "нейлоновая оплётка",
        },
        final_price=790,
    )


def _png_bytes() -> bytes:
    image = Image.new("RGB", (900, 900), "#f8fafc")
    return _image_bytes(image)


def _image_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_live_openrouter_text_job_renders_export_zip(tmp_path) -> None:
    api_key = _skip_without_live_flag()
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    _configure_openrouter(storage, api_key, images=False)

    job = asyncio.run(
        JobService(storage).create_job(
            CreateJobRequest(
                product=_product(),
                brief="Сделай аккуратную карточку WB без неподтверждённых обещаний.",
                allow_generate_without_photos=True,
            )
        )
    )

    assert job.status == "succeeded"
    assert job.result is not None
    assert job.result.provider == "openrouter"
    assert len(job.result.slides) == 10
    assert not any("fallback" in warning.lower() for warning in job.result.warnings)
    assert any("OpenRouter" in item for item in job.result.info)
    assert job.result.export_asset_id

    export_path, media_type = storage.get_asset(job.result.export_asset_id)
    assert media_type == "application/zip"
    with zipfile.ZipFile(export_path) as zf:
        names = set(zf.namelist())
        assert "slides/slide_01.png" in names
        assert "slides/slide_10.png" in names
        assert "rich/block_01.png" in names
        assert "rich/preview.html" not in names
        slide = Image.open(io.BytesIO(zf.read("slides/slide_01.png")))
        rich = Image.open(io.BytesIO(zf.read("rich/block_01.png")))
    assert slide.format == "PNG"
    assert slide.size == (900, 1200)
    assert rich.format == "PNG"
    assert rich.size == (1440, 900)


def test_live_openrouter_image_job_can_store_background_asset(tmp_path) -> None:
    api_key = _skip_without_live_flag()
    if not _truthy(os.environ.get("RUN_OPENROUTER_IMAGE_E2E")):
        pytest.skip("set RUN_OPENROUTER_IMAGE_E2E=1 to run live OpenRouter image test")

    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    _configure_openrouter(storage, api_key, images=True)
    source_id = storage.add_staging_asset(
        "live-openrouter",
        "source.png",
        _png_bytes(),
        "image/png",
    )

    job = asyncio.run(
        JobService(storage).create_job(
            CreateJobRequest(
                product=_product(),
                brief="Проверь генерацию фонового изображения только для hero-слайда.",
                source_image_asset_ids=[source_id],
            )
        )
    )

    assert job.status == "succeeded"
    assert job.result is not None
    assert job.result.provider == "openrouter"
    assert any(slide.background_asset_id for slide in job.result.slides)
    assert not any(
        "все запросы к openrouter image model завершились ошибкой" in warning.lower()
        for warning in job.result.warnings
    )
