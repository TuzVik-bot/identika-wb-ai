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
    assert "Очистить текст" in page_html
    assert "Очистить картинку" in page_html
    assert "button--danger-outline" in page_html
    assert "Удалить проект" in page_html
    assert 'class="button button--danger"' in page_html

    # E. Check for inline Rich preview workspace iframe
    assert "rich-workspace" in page_html
    assert "rich-iframe-preview" in page_html
    assert "Rich-контент предпросмотр" in page_html
    assert "Скачать Rich ZIP" in page_html

    # F. Check for the Javascript tab switcher script
    assert "const tabs = document.querySelectorAll('.job-tabs .tab-btn')" in page_html
    assert "const panels = document.querySelectorAll('.tab-panel')" in page_html

    # G. Check the side package contents lists are displayed under export tab
    assert "manifest.json" in page_html
    assert "package-files-list" in page_html
    assert "export-contract-list" in page_html
    assert "PNG 900×1200" in page_html
    assert "PNG 1440×900" in page_html
    assert "только preview, не в ZIP" in page_html
    assert "slides/slide_01.png" in page_html


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


def test_templates_page_saves_category_template(client: TestClient) -> None:
    page = client.get("/templates")
    assert page.status_code == 200
    assert "Шаблоны" in page.text
    assert "Кабель: техно-рамка" in page.text
    assert "Электроника: чистый техно" in page.text
    assert "Свет и интерьер: нижний акцент" in page.text
    assert 'data-template-field="template_id" name="template_id" value="cable-custom"' in page.text
    assert 'data-template-field="keywords" name="keywords"' in page.text
    assert 'data-live-preview' in page.text
    assert "template-copy-btn" in page.text
    assert 'data-template-id="cable-default"' in page.text
    assert 'data-template-keywords=' in page.text
    assert "Взять за основу" in page.text

    saved = client.post(
        "/templates",
        data={
            "template_id": "case-default",
            "name": "Чехол: чистая карточка",
            "category": "чехол",
            "keywords": "чехол, case",
            "accent_color": "#2563eb",
            "frame_style": "thin",
            "title_position": "top",
            "photo_treatment": "fit",
        },
    )
    assert saved.status_code == 303

    updated = client.get("/templates")
    assert updated.status_code == 200
    assert "Чехол: чистая карточка" in updated.text
    assert "чехол" in updated.text
    assert "case" in updated.text


def test_create_page_keeps_selected_category_template(client: TestClient) -> None:
    page = client.get("/create?account_id=1&q=товар&brief=Светлый фон&category_template_id=cable-default")
    assert page.status_code == 200
    assert 'id="category-template-select" name="category_template_id"' in page.text
    assert 'value="cable-default" selected' in page.text
    assert 'name="category_template_id" class="category-template-id-field" value="cable-default"' in page.text
    assert "/wb/generate" in page.text


def test_templates_page_deletes_custom_but_not_builtin_template(client: TestClient) -> None:
    created = client.post(
        "/templates",
        data={
            "template_id": "delete-me",
            "name": "Временный шаблон",
            "category": "временный",
            "keywords": "временный",
            "accent_color": "#2563eb",
            "frame_style": "thin",
            "title_position": "top",
            "photo_treatment": "fit",
        },
    )
    assert created.status_code == 303
    assert "Временный шаблон" in client.get("/templates").text

    deleted = client.post("/templates/delete-me/delete")
    assert deleted.status_code == 303
    page = client.get(deleted.headers["location"])
    assert "Шаблон удалён" in page.text
    assert "Временный шаблон" not in page.text

    blocked = client.post("/templates/cable-default/delete")
    assert blocked.status_code == 303
    page = client.get(blocked.headers["location"])
    assert "Встроенный шаблон нельзя удалить" in page.text
    assert "Кабель: техно-рамка" in page.text

    missing = client.post("/templates/no-such-template/delete")
    assert missing.status_code == 303
    page = client.get(missing.headers["location"])
    assert "Шаблон не найден или уже удалён" in page.text


def test_templates_page_edits_existing_template(client: TestClient) -> None:
    created = client.post(
        "/templates",
        data={
            "template_id": "edit-me",
            "name": "Старое имя",
            "category": "старая",
            "keywords": "старое",
            "accent_color": "#2563eb",
            "frame_style": "thin",
            "title_position": "top",
            "photo_treatment": "fit",
        },
    )
    assert created.status_code == 303

    edited = client.post(
        "/templates",
        data={
            "template_id": "edit-me",
            "name": "Новое имя",
            "category": "новая",
            "keywords": "новое, fresh",
            "accent_color": "#0f766e",
            "frame_style": "accent",
            "title_position": "left",
            "photo_treatment": "expand_square",
        },
    )
    assert edited.status_code == 303
    page = client.get("/templates")
    assert "Новое имя" in page.text
    assert "новая" in page.text
    assert "fresh" in page.text
    assert "Старое имя" not in page.text


def test_templates_page_refuses_builtin_overwrite(client: TestClient) -> None:
    response = client.post(
        "/templates",
        data={
            "template_id": "cable-default",
            "name": "Перезаписанный кабель",
            "category": "сломанная",
            "keywords": "сломанная",
            "accent_color": "#2563eb",
            "frame_style": "none",
            "title_position": "bottom",
            "photo_treatment": "fit",
        },
    )
    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert "Встроенный ID защищён" in page.text
    assert "Кабель: техно-рамка" in page.text
    assert "Перезаписанный кабель" not in page.text
