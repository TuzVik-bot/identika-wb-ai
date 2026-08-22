from __future__ import annotations

import io
import zipfile
from pathlib import PurePosixPath

import pytest
from fastapi.testclient import TestClient

from identika.app import create_app
from identika.config import settings
from identika.providers.mock import MockProvider
from identika.providers.openrouter import OpenRouterProvider, _TextPlan
from identika.services.wb_content import WBContentClient
from identika.services.wb_tool import WBToolClient

PRODUCT = {
    "store_slug": "api",
    "sku_id": 1,
    "nm_id": 4242,
    "title": "Тестовый товар",
    "subject_name": "Тест",
    "characteristics": {"Питание": "USB"},
}


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    settings.identika_db_path = tmp_path / "identika.sqlite"
    settings.identika_assets_dir = tmp_path / "assets"
    settings.identika_provider = "mock"
    settings.identika_public_base_path = ""
    settings.openrouter_api_key = ""
    app = create_app()
    return TestClient(app, follow_redirects=False)


def _job_id_from_location(location: str) -> str:
    return PurePosixPath(location).name


def test_auto_approve_finalizes_job_in_one_call(client: TestClient) -> None:
    created = client.post(
        "/v1/generation/jobs",
        json={"product": PRODUCT, "auto_approve": True},
    )
    assert created.status_code == 200
    job_id = created.json()["id"]

    status = client.get(f"/v1/generation/jobs/{job_id}")
    assert status.json()["status"] == "approved"

    result = client.get(f"/v1/generation/jobs/{job_id}/result").json()
    assert result["quality_mode"] == "final"
    assert all(slide["png_asset_id"] for slide in result["slides"])

    export = client.get(f"/v1/generation/jobs/{job_id}/export")
    assert export.status_code == 200
    with zipfile.ZipFile(io.BytesIO(export.content)) as zf:
        assert "seo/seo.txt" in zf.namelist()
        assert "slides/slide_01.png" in zf.namelist()


@pytest.mark.no_photo_inject
def test_auto_approve_without_photos_keeps_job_open_with_warning(
    client: TestClient,
    monkeypatch,
) -> None:
    async def no_photos(job_id: str, product, storage):
        return product, []

    monkeypatch.setattr("identika.services.jobs.download_product_images", no_photos)
    created = client.post(
        "/v1/generation/jobs",
        json={"product": PRODUCT, "auto_approve": True, "allow_generate_without_photos": True},
    )
    job_id = created.json()["id"]
    job = client.get(f"/v1/generation/jobs/{job_id}").json()
    assert job["status"] == "succeeded"
    result = client.get(f"/v1/generation/jobs/{job_id}/result").json()
    assert any("Авто-approve не выполнен" in warning for warning in result["warnings"])


def test_provider_override_to_openrouter_without_key_warns(client: TestClient) -> None:
    created = client.post(
        "/v1/generation/jobs",
        json={"product": PRODUCT, "provider": "openrouter"},
    )
    assert created.status_code == 200
    job_id = created.json()["id"]
    result = client.get(f"/v1/generation/jobs/{job_id}/result").json()
    assert any("OPENROUTER_API_KEY пуст" in warning for warning in result["warnings"])


def test_mock_provider_fills_seo_draft_texts() -> None:
    import asyncio

    from identika.models import CreateJobRequest, ProductContext

    result = asyncio.run(
        MockProvider().generate(
            CreateJobRequest(product=ProductContext.model_validate(PRODUCT))
        )
    )
    assert result.seo.title
    assert len(result.seo.title) <= 60
    assert result.seo.description
    assert result.seo.keywords


def test_openrouter_text_plan_applies_seo() -> None:
    import asyncio

    from identika.models import CreateJobRequest, ProductContext

    provider = OpenRouterProvider()
    request = CreateJobRequest(product=ProductContext.model_validate(PRODUCT))
    result = asyncio.run(MockProvider().generate(request))
    plan = _TextPlan.model_validate(
        {
            "slides": [
                {"index": i, "title": f"Заголовок {i}", "subtitle": "подзаголовок", "bullets": []}
                for i in range(1, 11)
            ],
            "seo": {
                "title": "Ночник проектор звёздного неба USB для детской",
                "description": "Проектор звёздного неба с 7 режимами и таймером.",
                "keywords": ["ночник проектор", "звёздное небо"],
            },
        }
    )
    provider._apply_text_plan(result, plan)
    assert result.seo.title == "Ночник проектор звёздного неба USB для детской"
    assert result.seo.keywords == ["ночник проектор", "звёздное небо"]


def test_parse_text_plan_accepts_seo_field() -> None:
    content = '{"slides": [{"index": 1, "title": "t"}, ...]}'
    provider = OpenRouterProvider()
    plan = provider._parse_text_plan(
        '{"seo": {"title": "T", "description": "D", "keywords": ["k"]}, '
        '"slides": [' + ",".join(f'{{"index": {i}, "title": "t{i}"}}' for i in range(1, 11)) + "]}"
    )
    assert plan.seo is not None
    assert plan.seo.title == "T"
    assert plan.seo.keywords == ["k"]


def test_generate_from_nm_id_uses_content_card(client: TestClient, monkeypatch) -> None:
    async def fake_card(self, nm_id: int):
        return {
            "nmID": nm_id,
            "vendorCode": "NM-1",
            "title": "Карточка из Content API",
            "subjectName": "Дом и интерьер",
            "characteristics": [
                {"name": "Цвет", "params": ["белый", "чёрный"]},
            ],
            "photos": [{"big": "https://cdn.wb.ru/photo-1.jpg"}],
        }

    monkeypatch.setattr(WBContentClient, "product_card", fake_card)
    response = client.post("/wb/generate-nm", data={"nm_id": "4242", "brief": "светлый фон"})
    assert response.status_code == 303
    job_id = _job_id_from_location(response.headers["location"])
    job = client.get(f"/v1/generation/jobs/{job_id}").json()
    assert job["nm_id"] == 4242
    result = client.get(f"/v1/generation/jobs/{job_id}/result").json()
    assert result["product"]["title"] == "Карточка из Content API"
    assert result["product"]["characteristics"]["Цвет"] == "белый, чёрный"


def test_generate_from_nm_id_without_card_falls_back_to_cdn_title(
    client: TestClient, monkeypatch
) -> None:
    async def no_card(self, nm_id: int):
        return None

    monkeypatch.setattr(WBContentClient, "product_card", no_card)
    response = client.post("/wb/generate-nm", data={"nm_id": "99"})
    assert response.status_code == 303
    job_id = _job_id_from_location(response.headers["location"])
    result = client.get(f"/v1/generation/jobs/{job_id}/result").json()
    assert result["product"]["title"] == "Товар WB 99"


def test_generate_from_nm_id_rejects_bad_nm(client: TestClient) -> None:
    response = client.post("/wb/generate-nm", data={"nm_id": "0"})
    assert response.status_code == 303
    assert "/create" in response.headers["location"]


def test_batch_creates_multiple_jobs(client: TestClient) -> None:
    response = client.post(
        "/v1/generation/jobs/batch",
        json={
            "jobs": [
                {"product": PRODUCT, "allow_generate_without_photos": True, "auto_approve": True},
                {"product": {**PRODUCT, "nm_id": 55, "sku_id": 2}},
                {"product": {"store_slug": "x", "title": "Без фото"}},
            ]
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["created"] == 2
    assert len(data["items"]) == 2
    assert len(data["errors"]) == 1
    first_id = data["items"][0]["id"]
    job = client.get(f"/v1/generation/jobs/{first_id}").json()
    assert job["status"] == "approved"


def test_batch_rejects_empty_and_oversized_lists(client: TestClient) -> None:
    assert client.post("/v1/generation/jobs/batch", json={"jobs": []}).status_code == 422
    big = [{"product": PRODUCT, "allow_generate_without_photos": True} for _ in range(21)]
    assert client.post("/v1/generation/jobs/batch", json={"jobs": big}).status_code == 422


def test_upload_to_wb_prefers_content_api(client: TestClient, monkeypatch) -> None:
    client.app.state.jobs.storage.set_settings({"wb_content_api_token": "content-token"})

    async def fake_upload_media(self, nm_id: int, photo_urls: list[str]) -> dict:
        self.captured = (nm_id, list(photo_urls))
        return {"ok": True, "upload_id": "wb-upload-1"}

    async def must_not_run(self, job, public_base_url: str = "") -> dict:
        raise AssertionError("WB Tool upload must not be called when Content API succeeds")

    monkeypatch.setattr(WBContentClient, "upload_media", fake_upload_media)
    monkeypatch.setattr(WBToolClient, "upload_job", must_not_run)

    created = client.post("/v1/generation/jobs", json={"product": PRODUCT, "auto_approve": True})
    job_id = created.json()["id"]

    upload = client.post(f"/jobs/{job_id}/upload-to-wb")
    assert upload.status_code == 303
    assert "upload=ok" in upload.headers["location"]
    assert "upload_via=wb_content" in upload.headers["location"]


def test_upload_to_wb_falls_back_to_wb_tool_on_content_failure(
    client: TestClient, monkeypatch
) -> None:
    client.app.state.jobs.storage.set_settings({"wb_content_api_token": "content-token"})

    async def failing_upload_media(self, nm_id: int, photo_urls: list[str]) -> dict:
        return {"ok": False, "reason": "http", "status": 401, "detail": "denied"}

    async def tool_upload(self, job, public_base_url: str = "") -> dict:
        return {"ok": True}

    monkeypatch.setattr(WBContentClient, "upload_media", failing_upload_media)
    monkeypatch.setattr(WBToolClient, "upload_job", tool_upload)

    created = client.post("/v1/generation/jobs", json={"product": PRODUCT, "auto_approve": True})
    job_id = created.json()["id"]
    upload = client.post(f"/jobs/{job_id}/upload-to-wb")
    assert upload.status_code == 303
    assert "upload=ok" in upload.headers["location"]
    assert "upload_via" not in upload.headers["location"]


def test_wb_generate_form_supports_auto_approve(client: TestClient, monkeypatch) -> None:
    async def fake_context(self, sku_id: int, account_id: int | None = None) -> dict:
        return {"store_slug": "demo", "sku_id": sku_id, "nm_id": 4242, "title": "Форма WB"}

    async def fake_resolve(self, sku_id, account_id, product):
        product.nm_id = 4242
        return product, []

    async def fake_media(self, sku_id: int, account_id: int | None = None) -> list[str]:
        return []

    async def no_content(self, nm_id):
        return []

    monkeypatch.setattr(WBToolClient, "product_context", fake_context)
    monkeypatch.setattr(WBToolClient, "resolve_product_images", fake_resolve)
    monkeypatch.setattr(WBToolClient, "product_media_urls", fake_media)
    monkeypatch.setattr(WBContentClient, "product_photo_urls", no_content)

    response = client.post(
        "/wb/generate",
        data={"account_id": "1", "sku_id": "7", "auto_approve": "1"},
    )
    assert response.status_code == 303
    job_id = _job_id_from_location(response.headers["location"])
    job = client.get(f"/v1/generation/jobs/{job_id}").json()
    assert job["status"] == "approved"
