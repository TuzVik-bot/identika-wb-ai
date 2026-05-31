from __future__ import annotations

from typing import Any

import httpx

from identika.config import settings
from identika.models import GenerationResult, JobRecord


def build_upload_payload(job: JobRecord, public_base_url: str = "") -> dict[str, Any]:
    if not job.result:
        raise ValueError("job has no result")
    result: GenerationResult = job.result
    product = result.product
    base = public_base_url.rstrip("/")
    assets = []
    for slide in result.slides:
        if slide.asset_id:
            assets.append(
                {
                    "kind": "slide",
                    "index": slide.index,
                    "asset_id": slide.asset_id,
                    "url": f"{base}/v1/assets/{slide.asset_id}" if base else f"/v1/assets/{slide.asset_id}",
                }
            )
    payload: dict[str, Any] = {
        "job_id": job.id,
        "account_id": product.account_id,
        "nm_id": product.nm_id,
        "sku_id": product.sku_id,
        "store_slug": product.store_slug,
        "title": product.title,
        "assets": assets,
    }
    if result.export_asset_id:
        export_url = (
            f"{base}/v1/generation/jobs/{job.id}/export"
            if base
            else f"/v1/generation/jobs/{job.id}/export"
        )
        payload["export_url"] = export_url
        payload["export_asset_id"] = result.export_asset_id
    return payload


class WBToolClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.wb_tool_base_url).rstrip("/")

    async def accounts(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(f"{self.base_url}/api/ai/accounts")
        response.raise_for_status()
        return response.json().get("items", [])

    async def products(self, account_id: int | None = None, q: str = "", limit: int = 100) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if account_id:
            params["account_id"] = account_id
        if q:
            params["q"] = q
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{self.base_url}/api/ai/products", params=params)
        response.raise_for_status()
        return response.json()

    async def product_context(self, sku_id: int, account_id: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if account_id:
            params["account_id"] = account_id
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{self.base_url}/api/ai/products/{sku_id}/context", params=params)
        response.raise_for_status()
        return response.json()

    async def upload_job(self, job: JobRecord, public_base_url: str = "") -> dict[str, Any]:
        """Upload approved job media to WB Tool.

        Contract: POST /api/ai/jobs/{job_id}/upload with manifest metadata,
        account_id, nm_id, sku_id, export URL and slide asset list.
        """
        payload = build_upload_payload(job, public_base_url)
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.base_url}/api/ai/jobs/{job.id}/upload",
                json=payload,
            )
        if response.status_code >= 400:
            return {"ok": False, "status": response.status_code, "detail": response.text[:1000]}
        data = response.json()
        if isinstance(data, dict) and "ok" not in data:
            data["ok"] = True
        return data
