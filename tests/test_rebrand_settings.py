from __future__ import annotations

import asyncio
import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from identika.app import create_app
from identika.config import EffectiveSettings, settings
from identika.models import CreateJobRequest, ProductContext
from identika.services.jobs import JobService
from identika.services.product_images import (
    _is_valid_product_image,
    download_product_images,
    ensure_product_image_urls,
)
from identika.services.wb_cdn import wb_basket_id, wb_image_url_candidates
from identika.storage import Storage
from identika.ui_labels import job_status_label


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


def test_dashboard_uses_editorial_cabinet_not_ai_hero(client: TestClient) -> None:
    home = client.get("/")
    assert home.status_code == 200
    text = home.text
    assert 'class="cabinet-header"' in text
    assert "profile-hero__avatar" not in text
    assert ">ai<" not in text
    assert "metric-card--accent" not in text


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
    job = asyncio.run(
        service.create_job(
            CreateJobRequest(
                product=ProductContext(title="Тест"),
                allow_generate_without_photos=True,
            )
        )
    )
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
    assert any("Режим качества: preview" in item for item in result["info"])


def test_download_product_images_stores_local_assets(tmp_path, monkeypatch) -> None:
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "530000000a49444154789c6260000000020001e221bc330000000049454e44ae426082"
    )

    class FakeResponse:
        status_code = 200

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
    updated, warnings = asyncio.run(download_product_images("job123", product, storage))
    assert updated.images[0].asset_id
    assert not warnings
    path, media_type = storage.get_asset(updated.images[0].asset_id)
    assert media_type == "image/png"
    assert path.read_bytes().startswith(b"\x89PNG")


def test_settings_preserves_masked_api_key(client: TestClient, tmp_path) -> None:
    client.post(
        "/settings",
        data={
            "provider": "openrouter",
            "openrouter_api_key": "sk-or-initial-key-9999",
            "openrouter_text_model": "test/text-model",
            "openrouter_image_model": "test/image-model",
        },
    )
    save = client.post(
        "/settings",
        data={
            "provider": "openrouter",
            "openrouter_api_key": "••••9999",
            "openrouter_text_model": "test/text-model",
            "openrouter_image_model": "test/image-model",
        },
    )
    assert save.status_code == 303
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    eff = EffectiveSettings.resolve(storage)
    assert eff.openrouter_api_key == "sk-or-initial-key-9999"


def test_dashboard_shows_russian_status_and_running_metrics(client: TestClient) -> None:
    demo = client.post("/demo")
    assert demo.status_code == 303
    home = client.get("/")
    assert home.status_code == 200
    assert "Готово" in home.text
    assert "В работе" in home.text
    assert "status-succeeded" in home.text
    assert job_status_label("succeeded") == "Готово"
    assert job_status_label("failed") == "Ошибка"


def test_settings_provider_applies_to_next_job(client: TestClient, tmp_path) -> None:
    client.post(
        "/settings",
        data={
            "provider": "mock",
            "openrouter_api_key": "",
            "openrouter_text_model": "test/text-model",
            "openrouter_image_model": "test/image-model",
        },
    )
    demo = client.post("/demo")
    job_id = demo.headers["location"].split("/")[-1]
    result = client.get(f"/v1/generation/jobs/{job_id}/result").json()
    assert result["provider"] == "mock"

    client.post(
        "/settings",
        data={
            "provider": "openrouter",
            "openrouter_api_key": "sk-or-test-key",
            "openrouter_text_model": "test/text-model",
            "openrouter_image_model": "test/image-model",
        },
    )
    demo2 = client.post("/demo")
    job_id2 = demo2.headers["location"].split("/")[-1]
    result2 = client.get(f"/v1/generation/jobs/{job_id2}/result").json()
    assert result2["provider"] == "openrouter"


def test_job_page_has_generation_meta_and_rerender(client: TestClient) -> None:
    demo = client.post("/demo")
    job_id = demo.headers["location"].split("/")[-1]
    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200
    assert "Информация о генерации" in page.text
    assert "Пересобрать слайды" in page.text
    assert "Статус генерации" in page.text or "mock" in page.text


def test_is_valid_product_image_rejects_tiny_or_non_image_payload() -> None:
    assert not _is_valid_product_image(b"")
    assert not _is_valid_product_image(b"not-an-image")
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "530000000a49444154789c6260000000020001e221bc330000000049454e44ae426082"
    )
    assert _is_valid_product_image(png)


def test_wb_cdn_url_candidates_from_nm_id() -> None:
    nm_id = 123_456_789
    vol = nm_id // 100_000
    urls = wb_image_url_candidates(nm_id, 1)
    assert wb_basket_id(vol) in urls[0]
    assert f"/vol{vol}/part{nm_id // 1000}/{nm_id}/images/big/1." in urls[0]
    assert any(url.endswith(".webp") for url in urls)
    assert any("wbbasket.ru" in url for url in urls)


def test_ensure_product_image_urls_uses_nm_id_when_empty() -> None:
    product = ProductContext(title="Тест", nm_id=2002, images=[])
    updated = ensure_product_image_urls(product)
    assert len(updated.images) == 5
    assert all(img.url and "2002" in img.url for img in updated.images)


def test_rerender_job_rebuilds_svg_with_embedded_images(tmp_path, monkeypatch) -> None:
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "530000000a49444154789c6260000000020001e221bc330000000049454e44ae426082"
    )

    class FakeResponse:
        status_code = 200

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
    service = JobService(storage)
    job = asyncio.run(
        service.create_job(
            CreateJobRequest(product=ProductContext(title="Тест", nm_id=2002, images=[]))
        )
    )
    slide_path, _ = storage.get_asset(job.result.slides[0].asset_id)
    assert "ТОВАР" not in slide_path.read_text(encoding="utf-8")

    rerendered = asyncio.run(service.rerender_job(job.id))
    export_path, _ = storage.get_asset(rerendered.result.export_asset_id)
    with zipfile.ZipFile(export_path) as zf:
        exported = Image.open(io.BytesIO(zf.read("slides/slide_01.png")))
    assert exported.format == "PNG"
    assert exported.size == (900, 1200)
