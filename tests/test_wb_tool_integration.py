from __future__ import annotations

import asyncio
from pathlib import PurePosixPath

import httpx
import pytest
from fastapi.testclient import TestClient

from identika.app import create_app
from identika.config import settings
from identika.models import JobRecord, ProductContext, ProductImage
from identika.services.wb_tool import (
    WBToolClient,
    build_upload_payload,
    merge_context_images,
    upload_redirect_query,
    _urls_from_media_payload,
)


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    settings.identika_db_path = tmp_path / "identika.sqlite"
    settings.identika_assets_dir = tmp_path / "assets"
    settings.identika_provider = "mock"
    settings.identika_public_base_path = ""
    app = create_app()
    return TestClient(app, follow_redirects=False)


def _job_id_from_location(location: str) -> str:
    return PurePosixPath(location.split("?")[0]).name


def test_build_upload_payload_includes_manifest_and_urls(tmp_path) -> None:
    from identika.services.jobs import JobService
    from identika.models import CreateJobRequest

    storage = __import__("identika.storage", fromlist=["Storage"]).Storage(
        db_path=tmp_path / "db.sqlite",
        assets_dir=tmp_path / "assets",
    )
    service = JobService(storage)
    job = asyncio.run(
        service.create_job(CreateJobRequest(product=ProductContext(title="Тест", nm_id=2002, sku_id=1)))
    )
    job = service.approve(job.id)
    payload = build_upload_payload(job, "https://example.com/identika")
    assert payload["contract_version"] == "1.0"
    assert payload["nm_id"] == 2002
    assert payload["manifest"]["slides"]
    assert payload["rich_assets"]
    rich_block = next(item for item in payload["rich_assets"] if item["kind"] == "rich_block")
    assert rich_block["media_type"] == "image/png"
    assert rich_block["width"] == 1440
    assert rich_block["height"] == 900
    assert any(item["kind"] == "rich_zip" for item in payload["rich_assets"])
    assert not any(item["kind"] == "rich_html" for item in payload["rich_assets"])
    assert any(item["kind"] == "rich_pdf" for item in payload["rich_assets"])
    assert payload["export_url"].startswith("https://example.com/identika")
    assert payload["manifest_url"].endswith("/result")


def test_merge_context_images_preserves_manual_assets() -> None:
    product = ProductContext(
        title="Тест",
        images=[ProductImage(asset_id="manual-1", role="source")],
    )
    merged = merge_context_images(
        product,
        {"images": [{"url": "https://cdn.example/1.jpg", "role": "source"}]},
    )
    assert any(img.asset_id == "manual-1" for img in merged.images)
    assert any(img.url == "https://cdn.example/1.jpg" for img in merged.images)


def test_urls_from_media_payload_shapes() -> None:
    assert _urls_from_media_payload({"images": ["https://a/1.jpg"]}) == ["https://a/1.jpg"]
    assert _urls_from_media_payload({"items": [{"url": "https://b/2.webp"}]}) == ["https://b/2.webp"]


def test_upload_redirect_query_variants() -> None:
    assert upload_redirect_query({"ok": True}) == "upload=ok"
    staging = upload_redirect_query({"ok": False, "staging": True, "detail": "501"})
    assert staging.startswith("upload=staging")
    assert "upload_detail=" in staging
    err = upload_redirect_query({"ok": False, "detail": "timeout", "status": 503})
    assert "upload=error" in err
    assert "upload_status_code=503" in err


def test_resolve_product_images_uses_context_urls(monkeypatch) -> None:
    async def fake_context(self, sku_id: int, account_id: int | None = None) -> dict:
        return {
            "sku_id": sku_id,
            "nm_id": 4242,
            "title": "Товар",
            "images": [{"url": "https://wb.example/photo.jpg"}],
        }

    async def fake_media(self, sku_id: int, account_id: int | None = None) -> list[str]:
        return []

    monkeypatch.setattr(WBToolClient, "product_context", fake_context)
    monkeypatch.setattr(WBToolClient, "product_media_urls", fake_media)
    product, notes = asyncio.run(
        WBToolClient("http://wb.test").resolve_product_images(1, 1, ProductContext())
    )
    assert product.images[0].url == "https://wb.example/photo.jpg"
    assert not notes or "CDN" not in notes[0]


def test_resolve_product_images_uses_media_endpoint(monkeypatch) -> None:
    async def fake_context(self, sku_id: int, account_id: int | None = None) -> dict:
        return {"sku_id": sku_id, "nm_id": 4242, "title": "Товар", "images": []}

    async def fake_media(self, sku_id: int, account_id: int | None = None) -> list[str]:
        return ["https://wb.example/media-1.jpg", "https://wb.example/media-2.jpg"]

    monkeypatch.setattr(WBToolClient, "product_context", fake_context)
    monkeypatch.setattr(WBToolClient, "product_media_urls", fake_media)
    product, notes = asyncio.run(
        WBToolClient("http://wb.test").resolve_product_images(1, 1, ProductContext())
    )
    assert len(product.images) == 2
    assert "WB Tool" in notes[0]


def test_upload_job_handles_501_as_staging(tmp_path, monkeypatch) -> None:
    from identika.models import CreateJobRequest
    from identika.services.jobs import JobService
    from identika.storage import Storage

    storage = Storage(db_path=tmp_path / "db.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = asyncio.run(
        service.create_job(
            CreateJobRequest(
                product=ProductContext(
                    title="Тест",
                    nm_id=99,
                    sku_id=1,
                    images=[ProductImage(url="https://example.com/p.png", role="source")],
                )
            )
        )
    )
    job = service.approve(job.id)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url: str, json=None):
            if url.endswith("/upload/staging"):
                return httpx.Response(404)
            return httpx.Response(501, json={"detail": "WB media upload is not implemented yet"})

    monkeypatch.setattr("identika.services.wb_tool.httpx.AsyncClient", lambda *a, **k: FakeClient())

    result = asyncio.run(WBToolClient("http://wb.test").upload_job(job, "https://host"))
    assert result.get("staging") is True
    assert result.get("ok") is False
    assert result.get("export_url")


def test_upload_to_wb_staging_redirect(client: TestClient, monkeypatch) -> None:
    async def fake_upload(self, job: JobRecord, public_base_url: str = "") -> dict:
        return {
            "ok": False,
            "staging": True,
            "detail": "WB media upload is not implemented yet",
        }

    monkeypatch.setattr(WBToolClient, "upload_job", fake_upload)
    demo = client.post("/demo")
    job_id = _job_id_from_location(demo.headers["location"])
    client.post(f"/v1/generation/jobs/{job_id}/approve")
    upload = client.post(f"/jobs/{job_id}/upload-to-wb")
    assert upload.status_code == 303
    assert "upload=staging" in upload.headers["location"]
    page = client.get(upload.headers["location"])
    assert page.status_code == 200
    assert "ZIP" in page.text


def test_upload_to_wb_error_includes_detail(client: TestClient, monkeypatch) -> None:
    async def fake_upload(self, job: JobRecord, public_base_url: str = "") -> dict:
        return {"ok": False, "detail": "connection refused", "status": 503}

    monkeypatch.setattr(WBToolClient, "upload_job", fake_upload)
    demo = client.post("/demo")
    job_id = _job_id_from_location(demo.headers["location"])
    client.post(f"/v1/generation/jobs/{job_id}/approve")
    upload = client.post(f"/jobs/{job_id}/upload-to-wb")
    assert "upload=error" in upload.headers["location"]
    assert "upload_detail=" in upload.headers["location"]
