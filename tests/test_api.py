from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from fastapi.testclient import TestClient

from identika.app import create_app
from identika.config import settings
from identika.services.wb_tool import WBToolClient


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    settings.identika_db_path = tmp_path / "identika.sqlite"
    settings.identika_assets_dir = tmp_path / "assets"
    settings.identika_provider = "mock"
    settings.identika_public_base_path = ""

    async def fake_accounts(self) -> list[dict]:
        return [{"id": 1, "name": "Demo WB", "slug": "demo-wb", "wb_configured": True}]

    async def fake_products(self, account_id: int, q: str = "", limit: int = 100) -> dict:
        return {
            "items": [
                {
                    "sku_id": 1001,
                    "nm_id": 2002,
                    "vendor_code": "DEMO-1",
                    "title": "Тестовый товар WB",
                    "subject_name": "Дом и интерьер",
                }
            ]
        }

    async def fake_product_context(self, sku_id: int, account_id: int | None = None) -> dict:
        return {
            "account_id": account_id,
            "store_slug": "demo-wb",
            "sku_id": sku_id,
            "nm_id": 2002,
            "vendor_code": "DEMO-1",
            "title": "Тестовый товар WB",
            "subject_name": "Дом и интерьер",
        }

    monkeypatch.setattr(WBToolClient, "accounts", fake_accounts)
    monkeypatch.setattr(WBToolClient, "products", fake_products)
    monkeypatch.setattr(WBToolClient, "product_context", fake_product_context)

    app = create_app()
    return TestClient(app, follow_redirects=False)


def _job_id_from_location(location: str) -> str:
    return PurePosixPath(location).name


def test_health_and_generation_api_contract(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True

    created = client.post(
        "/v1/generation/jobs",
        json={
            "product": {
                "store_slug": "api",
                "sku_id": 1,
                "nm_id": 11,
                "title": "Тестовый товар",
                "subject_name": "Тест",
            }
        },
    )
    assert created.status_code == 200
    job_id = created.json()["id"]
    assert created.json()["result_url"] == f"/v1/generation/jobs/{job_id}/result"
    assert created.json()["export_url"] == f"/v1/generation/jobs/{job_id}/export"

    result = client.get(f"/v1/generation/jobs/{job_id}/result")
    assert result.status_code == 200
    assert len(result.json()["slides"]) == 10

    patched = client.patch(
        f"/v1/generation/jobs/{job_id}/result/text",
        json={"slides": [{"index": 1, "title": "Правка из API"}]},
    )
    assert patched.status_code == 200
    assert patched.json()["slides"][0]["title"] == "Правка из API"


def test_ui_smoke_pages_demo_redirect_edit_approve_export_and_assets(client: TestClient) -> None:
    home = client.get("/")
    assert home.status_code == 200
    assert "Identika" in home.text
    assert "Создать проект" in home.text
    assert "/create" in home.text

    create_page = client.get("/create?account_id=1&q=товар&brief=Светлый фон")
    assert create_page.status_code == 200
    assert "Ваш товар" in create_page.text
    assert "Подходит для разных категорий товаров" in create_page.text
    assert "3 шага" in create_page.text
    assert "Тестовый товар WB" in create_page.text
    assert "/wb/generate" in create_page.text

    wb_job = client.post("/wb/generate", data={"account_id": 1, "sku_id": 1001, "brief": "Светлый фон"})
    assert wb_job.status_code == 303
    assert wb_job.headers["location"].startswith("/jobs/")

    demo = client.post("/demo")
    assert demo.status_code == 303
    assert demo.headers["location"].startswith("/jobs/")

    job_id = _job_id_from_location(demo.headers["location"])

    job_page = client.get(f"/jobs/{job_id}")
    assert job_page.status_code == 200
    assert "Approve" in job_page.text
    assert "Export ZIP" in job_page.text
    assert "10 слайдов" in job_page.text
    assert f"/v1/generation/jobs/{job_id}/approve" in job_page.text
    assert f"/v1/generation/jobs/{job_id}/export" in job_page.text
    assert "/v1/assets/" in job_page.text

    result = client.get(f"/v1/generation/jobs/{job_id}/result")
    assert result.status_code == 200
    result_json = result.json()
    assert result_json["product"]["store_slug"] == "demo"
    assert len(result_json["slides"]) == 10

    slide_asset_id = result_json["slides"][0]["asset_id"]
    slide_asset = client.get(f"/v1/assets/{slide_asset_id}")
    assert slide_asset.status_code == 200
    assert slide_asset.headers["content-type"].startswith("image/svg+xml")

    edit = client.post(
        f"/jobs/{job_id}/slides/1/text",
        data={
            "title": "Новый заголовок",
            "subtitle": "Новый подзаголовок",
            "bullets": "Первый\nВторой",
        },
    )
    assert edit.status_code == 303
    assert edit.headers["location"].endswith(f"/jobs/{job_id}")

    updated_job_page = client.get(f"/jobs/{job_id}")
    assert updated_job_page.status_code == 200
    assert "Новый заголовок" in updated_job_page.text
    assert "Новый подзаголовок" in updated_job_page.text

    approved = client.post(f"/v1/generation/jobs/{job_id}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    approved_page = client.get(f"/jobs/{job_id}")
    assert approved_page.status_code == 200
    assert f"/jobs/{job_id}/slides/1/text" not in approved_page.text

    export = client.get(f"/v1/generation/jobs/{job_id}/export")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("application/zip")
    assert f"identika_{job_id}.zip" in export.headers["content-disposition"]

    locked_edit = client.post(
        f"/jobs/{job_id}/slides/1/text",
        data={"title": "Не должно примениться", "subtitle": "", "bullets": ""},
    )
    assert locked_edit.status_code == 409


def test_negative_404_and_409_responses(client: TestClient) -> None:
    assert client.get("/jobs/missing").status_code == 404
    assert client.get("/v1/generation/jobs/missing").status_code == 404
    assert client.get("/v1/generation/jobs/missing/result").status_code == 404
    assert client.post("/v1/generation/jobs/missing/approve").status_code == 404
    assert client.get("/v1/assets/missing").status_code == 404

    demo = client.post("/demo")
    job_id = _job_id_from_location(demo.headers["location"])

    upload = client.post(f"/jobs/{job_id}/upload-to-wb")
    assert upload.status_code == 409
    assert "after approve" in upload.json()["detail"]
