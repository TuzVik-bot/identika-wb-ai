from __future__ import annotations

import asyncio
import zipfile

from identika.config import settings
from identika.models import CreateJobRequest, ProductContext, ResultTextPatch
from identika.providers.mock import MockProvider
from identika.services.jobs import JobService
from identika.storage import Storage


def product() -> ProductContext:
    settings.identika_provider = "mock"
    return ProductContext(
        store_slug="test",
        sku_id=7,
        nm_id=7001,
        title="Ночник-проектор звёздного неба",
        subject_name="Дом и интерьер",
        characteristics={"Питание": "USB"},
    )


def test_mock_provider_returns_exactly_ten_slides_with_roles() -> None:
    result = asyncio.run(MockProvider().generate(CreateJobRequest(product=product())))

    assert len(result.slides) == 10
    assert result.slides[0].role == "hero"
    assert [slide.role for slide in result.slides[1:5]] == ["description"] * 4
    assert [slide.role for slide in result.slides[5:]] == ["white_background"] * 5
    assert len(result.rich.blocks) == 10


def test_job_service_exports_assets_pdf_manifest_and_zip(tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)

    job = asyncio.run(service.create_job(CreateJobRequest(product=product())))

    assert job.status == "succeeded"
    assert job.result is not None
    assert len(job.result.slides) == 10
    assert job.result.rich.pdf_asset_id
    assert job.result.rich.html_asset_id
    assert job.result.export_asset_id

    export_path, media_type = storage.get_asset(job.result.export_asset_id)
    assert media_type == "application/zip"
    with zipfile.ZipFile(export_path) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "rich/preview.pdf" in names
        assert "rich/preview.html" in names
        assert "slides/slide_01.svg" in names
        assert "slides/slide_10.svg" in names


def test_approve_only_after_success(tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = asyncio.run(service.create_job(CreateJobRequest(product=product())))

    approved = service.approve(job.id)

    assert approved.status == "approved"
    assert approved.approved_at is not None


def test_result_does_not_include_known_secret_fields(tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = asyncio.run(service.create_job(CreateJobRequest(product=product())))

    dumped = job.result.model_dump_json() if job.result else ""

    assert "wb_api_token" not in dumped
    assert "b2b_client_secret" not in dumped
    assert "OPENROUTER_API_KEY" not in dumped


def test_patch_result_text_updates_manifest_export(tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = asyncio.run(service.create_job(CreateJobRequest(product=product())))

    updated = service.patch_result_text(
        job.id,
        ResultTextPatch(
            slides=[{"index": 1, "title": "Новый заголовок", "subtitle": "Новый подзаголовок"}],
            rich_blocks=[{"index": 1, "title": "Новый rich", "text": "Новый текст"}],
        ),
    )

    assert updated.result is not None
    assert updated.result.slides[0].title == "Новый заголовок"
    assert updated.result.rich.blocks[0].title == "Новый rich"

    export_path, _ = storage.get_asset(updated.result.export_asset_id)
    with zipfile.ZipFile(export_path) as zf:
        manifest = zf.read("manifest.json").decode("utf-8")
    assert "Новый заголовок" in manifest
