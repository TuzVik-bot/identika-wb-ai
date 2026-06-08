from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from identika.app import create_app
from identika.config import settings
from identika.models import CreateJobRequest, ProductContext, ProductImage
from identika.services.jobs import JobService
from identika.services.product_images import SourcePhotosRequiredError, validate_can_start_generation
from identika.services.rendering import render_slide_svg
from identika.models import SlideSpec
from identika.storage import Storage


def _png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (1, 1), "#ffffff").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    settings.identika_db_path = tmp_path / "identika.sqlite"
    settings.identika_assets_dir = tmp_path / "assets"
    settings.identika_provider = "mock"
    settings.identika_public_base_path = ""
    settings.identika_api_key = ""
    settings.identika_ui_password = ""
    return TestClient(create_app(), follow_redirects=False)


@pytest.mark.no_photo_inject
def test_validate_blocks_product_without_photos_or_nm_id() -> None:
    product = ProductContext(title="Пустой товар", sku_id=1)
    with pytest.raises(SourcePhotosRequiredError):
        validate_can_start_generation(product, allow_without_photos=False)


@pytest.mark.no_photo_inject
def test_api_create_job_returns_400_without_photos(client: TestClient) -> None:
    response = client.post(
        "/v1/generation/jobs",
        json={
            "product": {
                "store_slug": "test",
                "sku_id": 1,
                "title": "Без фото",
            },
            "allow_generate_without_photos": False,
        },
    )
    assert response.status_code == 400
    assert "фото" in response.json()["detail"].lower()


def test_api_create_job_with_uploaded_source_succeeds(client: TestClient) -> None:
    upload = client.post(
        "/v1/uploads/source-images",
        files=[("files", ("photo.png", _png_bytes(), "image/png"))],
    )
    assert upload.status_code == 200
    asset_id = upload.json()["asset_ids"][0]

    created = client.post(
        "/v1/generation/jobs",
        json={
            "product": {"store_slug": "test", "sku_id": 2, "title": "С фото"},
            "source_image_asset_ids": [asset_id],
        },
    )
    assert created.status_code == 200
    job_id = created.json()["id"]
    result = client.get(f"/v1/generation/jobs/{job_id}/result")
    assert result.status_code == 200
    svg_href = result.json()["slides"][0]["asset_id"]
    asset = client.get(f"/v1/assets/{svg_href}")
    assert asset.status_code == 200
    assert "Загрузите фото товара" not in asset.text
    assert "ТОВАР" not in asset.text


@pytest.mark.no_photo_inject
def test_render_slide_without_photo_shows_upload_message() -> None:
    slide = SlideSpec(index=1, role="hero", title="Тест", subtitle="Подзаголовок")
    svg = render_slide_svg(slide).decode("utf-8")
    assert "Загрузите фото товара" in svg
    assert "ТОВАР" not in svg


@pytest.mark.no_photo_inject
def test_job_fails_after_download_without_assets(tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    product = ProductContext(title="Нет CDN", nm_id=999999999, sku_id=1)
    with pytest.raises(SourcePhotosRequiredError):
        asyncio.run(
            service.create_job(
                CreateJobRequest(product=product, allow_generate_without_photos=False)
            )
        )
    jobs = storage.list_jobs()
    assert jobs
    assert jobs[0].status == "failed"


def test_attach_source_images_to_job_rerenders(tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = asyncio.run(
        service.create_job(
            CreateJobRequest(
                product=ProductContext(title="Демо"),
                allow_generate_without_photos=True,
            )
        )
    )
    assert job.result is not None
    job.result.product.images = []
    storage.update_result(job.id, job.result)
    staging_id = storage.add_staging_asset("sess", "manual.png", _png_bytes(), "image/png")
    updated = asyncio.run(service.attach_source_images_to_job(job.id, [staging_id]))
    assert updated.result is not None
    assert any(img.asset_id == staging_id for img in updated.result.product.images)
    slide_path, _ = storage.get_asset(updated.result.slides[0].asset_id)
    svg = slide_path.read_text(encoding="utf-8")
    assert "data:image/png;base64," in svg
    assert "Загрузите фото товара" not in svg
