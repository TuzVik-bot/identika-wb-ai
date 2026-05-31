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


def test_redesigned_job_page_elements(client: TestClient) -> None:
    # 1. Trigger demo job creation to get a redirect location
    demo = client.post("/demo")
    assert demo.status_code == 303
    assert demo.headers["location"].startswith("/jobs/")
    job_id = _job_id_from_location(demo.headers["location"])

    # 2. Get the job page and verify redesign elements
    job_page = client.get(f"/jobs/{job_id}")
    assert job_page.status_code == 200
    page_html = job_page.text

    # A. Check for wide layout class
    assert "job-layout--wide" in page_html

    # B. Check for interactive tabs buttons
    assert 'class="tab-btn is-active" data-tab="slides"' in page_html
    assert 'class="tab-btn" data-tab="rich"' in page_html
    assert 'class="tab-btn" data-tab="export"' in page_html

    # C. Check for tab content panel definitions
    assert 'id="tab-slides" class="tab-panel is-active"' in page_html
    assert 'id="tab-rich" class="tab-panel"' in page_html
    assert 'id="tab-export" class="tab-panel"' in page_html

    # D. Check for collapsible details slide editor
    assert "slide-edit-details" in page_html
    assert "Редактировать текст" in page_html
    assert "✎" in page_html

    # E. Check for inline Rich preview workspace iframe
    assert "rich-workspace" in page_html
    assert "rich-iframe-preview" in page_html
    assert "Rich-контент предпросмотр" in page_html

    # F. Check for the Javascript tab switcher script
    assert "const tabs = document.querySelectorAll('.job-tabs .tab-btn')" in page_html
    assert "const panels = document.querySelectorAll('.tab-panel')" in page_html

    # G. Check the side package contents lists are displayed under export tab
    assert "manifest.json" in page_html
    assert "package-files-list" in page_html


def test_edit_flow_with_accordion(client: TestClient) -> None:
    # Create demo job
    demo = client.post("/demo")
    job_id = _job_id_from_location(demo.headers["location"])

    # Verify editing applies correctly
    edit_response = client.post(
        f"/jobs/{job_id}/slides/1/text",
        data={
            "title": "Инновационный Свет",
            "subtitle": "7 ярких режимов проекции",
            "bullets": "Встроенный таймер\nПульт ДУ\nUSB-питание",
        },
    )
    assert edit_response.status_code == 303

    # View page and assert changes are in HTML
    updated = client.get(f"/jobs/{job_id}")
    assert updated.status_code == 200
    assert "Инновационный Свет" in updated.text
    assert "7 ярких режимов проекции" in updated.text
    assert "Встроенный таймер" in updated.text
