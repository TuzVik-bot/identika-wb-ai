from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from fastapi.testclient import TestClient

from identika.app import create_app
from identika.config import settings
from identika.models import JobRecord
from identika.services.wb_tool import WBToolClient


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    settings.identika_db_path = tmp_path / "identika.sqlite"
    settings.identika_assets_dir = tmp_path / "assets"
    settings.identika_provider = "mock"
    settings.identika_public_base_path = ""

    async def fake_upload_job(self, job: JobRecord, public_base_url: str = "") -> dict:
        assert job.status == "approved"
        assert job.result is not None
        assert job.result.product.nm_id is not None
        return {"ok": True, "uploaded": True}

    monkeypatch.setattr(WBToolClient, "upload_job", fake_upload_job)
    app = create_app()
    return TestClient(app, follow_redirects=False)


def _job_id_from_location(location: str) -> str:
    return PurePosixPath(location.split("?")[0]).name


def test_upload_to_wb_success_redirects_with_ok(client: TestClient) -> None:
    demo = client.post("/demo")
    job_id = _job_id_from_location(demo.headers["location"])
    client.post(f"/v1/generation/jobs/{job_id}/approve")

    upload = client.post(f"/jobs/{job_id}/upload-to-wb")
    assert upload.status_code == 303
    assert upload.headers["location"].endswith(f"/jobs/{job_id}?upload=ok")

    page = client.get(f"/jobs/{job_id}?upload=ok")
    assert page.status_code == 200
    assert "успешно отправлен" in page.text.lower()
