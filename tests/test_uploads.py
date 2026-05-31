from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from identika.app import create_app
from identika.config import settings
from identika.models import CreateJobRequest, ProductContext
from identika.services.jobs import JobService
from identika.storage import Storage


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    settings.identika_db_path = tmp_path / "identika.sqlite"
    settings.identika_assets_dir = tmp_path / "assets"
    settings.identika_provider = "mock"
    settings.identika_public_base_path = ""
    settings.identika_api_key = ""
    settings.identika_ui_password = ""
    app = create_app()
    return TestClient(app, follow_redirects=False)


def _png_bytes() -> bytes:
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "530000000a49444154789c6260000000020001e221bc330000000049454e44ae426082"
    )


def test_upload_rejects_too_many_files(client: TestClient) -> None:
    files = [("files", (f"photo{i}.png", _png_bytes(), "image/png")) for i in range(5)]
    response = client.post("/v1/uploads/source-images", files=files)
    assert response.status_code == 400
    assert "maximum 4" in response.json()["detail"]


def test_upload_rejects_invalid_mime(client: TestClient) -> None:
    response = client.post(
        "/v1/uploads/source-images",
        files=[("files", ("doc.txt", b"hello", "text/plain"))],
    )
    assert response.status_code == 400
    assert "unsupported image type" in response.json()["detail"]


def test_upload_success_returns_asset_ids(client: TestClient) -> None:
    response = client.post(
        "/v1/uploads/source-images",
        files=[("files", ("photo.png", _png_bytes(), "image/png"))],
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["asset_ids"]) == 1
    assert payload["session_id"]


def test_job_with_source_images_references_asset_in_svg(tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    asset_id = storage.add_staging_asset("test-session", "source.png", _png_bytes(), "image/png")
    service = JobService(storage)
    job = __import__("asyncio").run(
        service.create_job(
            CreateJobRequest(
                product=ProductContext(title="Тестовый товар"),
                source_image_asset_ids=[asset_id],
            )
        )
    )
    assert job.result is not None
    slide_path, _ = storage.get_asset(job.result.slides[0].asset_id)
    svg = slide_path.read_text(encoding="utf-8")
    assert f"/v1/assets/{asset_id}" in svg
    assert "ТОВАР" not in svg
    export_path, _ = storage.get_asset(job.result.export_asset_id)
    with zipfile.ZipFile(export_path) as zf:
        exported = zf.read("slides/slide_01.svg").decode("utf-8")
    assert "data:image/png;base64," in exported
