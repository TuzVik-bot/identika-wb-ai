from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from identika.app import create_app
from identika.config import settings
from identika.models import CreateJobRequest, ProductContext, ProductImage
from identika.services.jobs import JobService
from identika.services.product_images import (
    SourcePhotosRequiredError,
    download_product_images,
    validate_can_start_generation,
)
from identika.services.rendering import render_slide_svg
from identika.services.wb_tool import WBToolClient
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
def test_job_continues_after_wb_and_internet_search_without_assets(tmp_path, monkeypatch) -> None:
    class FakeResponse:
        status_code = 404
        headers = {"content-type": "application/json"}
        content = b""
        text = ""
        reason_phrase = "Not Found"

        def raise_for_status(self) -> None:
            raise RuntimeError("not found")

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url: str, **kwargs):
            if "api.openverse.org" in url:
                return FakeResponse()
            return FakeResponse()

    monkeypatch.setattr("identika.services.product_images.httpx.AsyncClient", lambda *a, **k: FakeClient())

    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    product = ProductContext(title="Нет CDN", nm_id=999999999, sku_id=1)
    job = asyncio.run(
        service.create_job(
            CreateJobRequest(product=product, allow_generate_without_photos=False)
        )
    )

    assert job.status == "succeeded"
    assert job.result is not None
    assert len(job.result.slides) == 10
    assert not job.result.product.images
    assert any("загрузите фото вручную" in warning.lower() for warning in job.result.warnings)
    slide_path, _ = storage.get_asset(job.result.slides[0].asset_id)
    assert "Загрузите фото товара" in slide_path.read_text(encoding="utf-8")


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


@pytest.mark.no_photo_inject
def test_download_product_images_falls_back_to_openverse_search(tmp_path, monkeypatch) -> None:
    png = _png_bytes()

    class FakeResponse:
        def __init__(
            self,
            status_code: int,
            *,
            json_data: dict | None = None,
            content: bytes = b"",
            content_type: str = "application/json",
        ) -> None:
            self.status_code = status_code
            self._json_data = json_data or {}
            self.content = content
            self.headers = {"content-type": content_type}
            self.text = ""
            self.reason_phrase = "OK"

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self) -> dict:
            return self._json_data

    class FakeClient:
        def __init__(self) -> None:
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True
            return None

        async def get(self, url: str, **kwargs):
            assert not self.closed
            if "api.openverse.org" in url:
                params = kwargs.get("params") or {}
                assert "Дизайнерская лампа" in params["q"]
                return FakeResponse(
                    200,
                    json_data={
                        "results": [
                            {"url": "https://cdn.example/lamp.png", "title": "lamp"},
                        ]
                    },
                )
            if url == "https://cdn.example/lamp.png":
                return FakeResponse(200, content=png, content_type="image/png")
            return FakeResponse(404)

    monkeypatch.setattr("identika.services.product_images.httpx.AsyncClient", lambda *a, **k: FakeClient())

    storage = Storage(db_path=tmp_path / "db.sqlite", assets_dir=tmp_path / "assets")
    product = ProductContext(
        title="Дизайнерская лампа",
        subject_name="Освещение",
        nm_id=999999999,
        sku_id=1,
    )
    updated, warnings = asyncio.run(download_product_images("job-openverse", product, storage))

    assert updated.images[0].url == "https://cdn.example/lamp.png"
    assert updated.images[0].asset_id
    assert any("Openverse" in warning for warning in warnings)
    path, media_type = storage.get_asset(updated.images[0].asset_id)
    assert media_type == "image/png"
    assert path.read_bytes().startswith(b"\x89PNG")


@pytest.mark.no_photo_inject
def test_wb_generation_accepts_internet_source_image_url(client: TestClient, monkeypatch) -> None:
    async def fake_context(self, sku_id: int, account_id: int | None = None) -> dict:
        return {
            "account_id": account_id,
            "store_slug": "demo",
            "sku_id": sku_id,
            "title": "Товар без фото WB",
            "images": [],
        }

    async def fake_media(self, sku_id: int, account_id: int | None = None) -> list[str]:
        return []

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/png"}
        content = _png_bytes()

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url: str):
            assert url == "https://images.example/product.png"
            return FakeResponse()

    monkeypatch.setattr(WBToolClient, "product_context", fake_context)
    monkeypatch.setattr(WBToolClient, "product_media_urls", fake_media)
    monkeypatch.setattr("identika.services.product_images.httpx.AsyncClient", lambda *a, **k: FakeClient())

    response = client.post(
        "/wb/generate",
        data={
            "account_id": "1",
            "sku_id": "77",
            "source_image_urls": "https://images.example/product.png",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")

    job_id = response.headers["location"].split("/")[-1]
    result = client.get(f"/v1/generation/jobs/{job_id}/result")
    assert result.status_code == 200
    product_images = result.json()["product"]["images"]
    assert product_images[0]["url"] == "https://images.example/product.png"
    assert product_images[0]["asset_id"]
