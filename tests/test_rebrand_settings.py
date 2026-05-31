from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from identika.app import create_app
from identika.config import EffectiveSettings, settings
from identika.models import CreateJobRequest, ProductContext
from identika.services.jobs import JobService
from identika.services.product_images import download_product_images
from identika.storage import Storage


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    settings.identika_db_path = tmp_path / "identika.sqlite"
    settings.identika_assets_dir = tmp_path / "assets"
    settings.identika_provider = "mock"
    settings.identika_public_base_path = ""
    settings.openrouter_api_key = ""
    app = create_app()
    return TestClient(app, follow_redirects=False)


def test_templates_use_identika_brand_not_aidentika(client: TestClient) -> None:
    for path in ("/", "/create", "/settings"):
        response = client.get(path)
        assert response.status_code == 200
        text = response.text
        assert "Identika" in text
        assert "Aidentika" not in text
        assert "aidentika" not in text


def test_dashboard_has_settings_quick_card_not_api(client: TestClient) -> None:
    home = client.get("/")
    assert home.status_code == 200
    assert "Настройки" in home.text
    assert "/settings" in home.text
    assert "Кабинет API" not in home.text
    assert '/health">API<' not in home.text


def test_settings_get_post_and_db_over_env(client: TestClient, tmp_path) -> None:
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Провайдер генерации" in page.text

    save = client.post(
        "/settings",
        data={
            "provider": "openrouter",
            "openrouter_api_key": "sk-or-test-key-1234",
            "openrouter_text_model": "test/text-model",
            "openrouter_image_model": "test/image-model",
            "enable_ai_images": "on",
        },
    )
    assert save.status_code == 303
    assert save.headers["location"].endswith("/settings?saved=ok")

    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    eff = EffectiveSettings.resolve(storage)
    assert eff.provider == "openrouter"
    assert eff.openrouter_api_key == "sk-or-test-key-1234"
    assert eff.openrouter_text_model == "test/text-model"
    assert eff.enable_ai_images is True

    settings.identika_provider = "mock"
    assert eff.provider == "openrouter"


def test_dynamic_routes_send_no_store_cache_control(client: TestClient) -> None:
    demo = client.post("/demo")
    job_id = demo.headers["location"].split("/")[-1]

    for path in (
        f"/jobs/{job_id}",
        f"/v1/generation/jobs/{job_id}",
        f"/v1/generation/jobs/{job_id}/result",
        "/health",
    ):
        response = client.get(path)
        assert response.status_code == 200
        cache_control = response.headers.get("cache-control", "")
        assert "no-store" in cache_control


def test_job_page_shows_result_even_if_status_running(client: TestClient, tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = asyncio.run(service.create_job(CreateJobRequest(product=ProductContext(title="Тест"))))
    assert job.result is not None

    with storage._connect() as conn:
        conn.execute("UPDATE jobs SET status=? WHERE id=?", ("running", job.id))

    page = client.get(f"/jobs/{job.id}")
    assert page.status_code == 200
    assert "10 слайдов" in page.text
    assert "Генерация выполняется" not in page.text


def test_mock_warnings_separate_from_info(client: TestClient) -> None:
    demo = client.post("/demo")
    job_id = demo.headers["location"].split("/")[-1]
    result = client.get(f"/v1/generation/jobs/{job_id}/result").json()
    assert any("Mock" in item for item in result["warnings"])
    assert result["info"] == []


def test_download_product_images_stores_local_assets(tmp_path, monkeypatch) -> None:
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "530000000a49444154789c6260000000020001e221bc330000000049454e44ae426082"
    )

    class FakeResponse:
        def __init__(self) -> None:
            self.headers = {"content-type": "image/png"}

        def raise_for_status(self) -> None:
            return None

        @property
        def content(self) -> bytes:
            return png

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url: str):
            return FakeResponse()

    monkeypatch.setattr("identika.services.product_images.httpx.AsyncClient", lambda *a, **k: FakeClient())

    storage = Storage(db_path=tmp_path / "db.sqlite", assets_dir=tmp_path / "assets")
    product = ProductContext(
        title="Тест",
        images=[{"url": "https://example.com/product.png", "role": "source"}],
    )
    updated = asyncio.run(download_product_images("job123", product, storage))
    assert updated.images[0].asset_id
    path, media_type = storage.get_asset(updated.images[0].asset_id)
    assert media_type == "image/png"
    assert path.read_bytes().startswith(b"\x89PNG")
