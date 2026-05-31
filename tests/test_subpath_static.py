from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from identika.app import create_app
from identika.config import settings


@pytest.fixture()
def subpath_client(tmp_path) -> TestClient:
    settings.identika_db_path = tmp_path / "identika.sqlite"
    settings.identika_assets_dir = tmp_path / "assets"
    settings.identika_provider = "mock"
    settings.identika_public_base_path = "/identika"
    settings.identika_ui_password = ""
    app = create_app()
    return TestClient(app, follow_redirects=False)


def test_dashboard_links_prefixed_static_css(subpath_client: TestClient) -> None:
    home = subpath_client.get("/")
    assert home.status_code == 200
    assert 'href="/identika/static/app.css"' in home.text
    assert "<title>Кабинет · Aidentika</title>" in home.text


def test_static_css_served_at_root_and_subpath(subpath_client: TestClient) -> None:
    root = subpath_client.get("/static/app.css")
    assert root.status_code == 200
    assert "text/css" in root.headers["content-type"]

    prefixed = subpath_client.get("/identika/static/app.css")
    assert prefixed.status_code == 200
    assert "text/css" in prefixed.headers["content-type"]
    assert ".profile-hero" in prefixed.text


def test_wb_tool_display_url_prefers_public_setting(tmp_path, monkeypatch) -> None:
    settings.identika_db_path = tmp_path / "identika.sqlite"
    settings.identika_assets_dir = tmp_path / "assets"
    settings.identika_public_base_path = ""
    monkeypatch.setattr(settings, "wb_tool_base_url", "http://127.0.0.1:8765")
    monkeypatch.setattr(settings, "wb_tool_public_url", "https://eurasia-transline.online")

    client = TestClient(create_app(), follow_redirects=False)
    home = client.get("/")
    assert home.status_code == 200
    assert "https://eurasia-transline.online" in home.text
    assert "127.0.0.1:8765" not in home.text
