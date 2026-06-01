from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import httpx

from identika.config import settings
from identika.models import GenerationResult, JobRecord, ProductContext, ProductImage

logger = logging.getLogger("identika.wb_tool")

# Optional WB Tool routes (forward-compatible). Primary source remains /context.
_PRODUCT_IMAGE_PATHS = ("media", "images", "photos")

# Staging upload when POST /jobs/{id}/upload returns 501 (not implemented in WB Tool yet).
STAGING_UPLOAD_PATH = "/api/ai/jobs/{job_id}/upload/staging"


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
                    "role": slide.role,
                    "asset_id": slide.asset_id,
                    "url": f"{base}/v1/assets/{slide.asset_id}" if base else f"/v1/assets/{slide.asset_id}",
                }
            )
    manifest = result.model_dump(mode="json")
    manifest.pop("export_asset_id", None)
    payload: dict[str, Any] = {
        "contract_version": "1.0",
        "job_id": job.id,
        "account_id": product.account_id,
        "nm_id": product.nm_id,
        "sku_id": product.sku_id,
        "store_slug": product.store_slug,
        "title": product.title,
        "assets": assets,
        "manifest": manifest,
        "slide_count": len(result.slides),
    }
    if result.export_asset_id:
        export_url = (
            f"{base}/v1/generation/jobs/{job.id}/export"
            if base
            else f"/v1/generation/jobs/{job.id}/export"
        )
        payload["export_url"] = export_url
        payload["export_asset_id"] = result.export_asset_id
    result_url = (
        f"{base}/v1/generation/jobs/{job.id}/result"
        if base
        else f"/v1/generation/jobs/{job.id}/result"
    )
    payload["manifest_url"] = result_url
    return payload


def _product_images_from_context(context: dict[str, Any]) -> list[ProductImage]:
    raw = context.get("images") or []
    images: list[ProductImage] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            images.append(ProductImage(url=item.strip(), role="source"))
            continue
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("src") or item.get("href")
        if not url:
            continue
        images.append(
            ProductImage(
                url=str(url),
                role=str(item.get("role") or "source"),
                alt=str(item.get("alt") or ""),
            )
        )
    return images


def _urls_from_media_payload(data: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(url: Any) -> None:
        if not isinstance(url, str):
            return
        clean = url.strip()
        if not clean or clean in seen:
            return
        seen.add(clean)
        urls.append(clean)

    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                add(item.get("url") or item.get("src") or item.get("href"))
        return urls

    if not isinstance(data, dict):
        return urls

    for key in ("images", "items", "urls", "photos", "media"):
        block = data.get(key)
        if block is None:
            continue
        if isinstance(block, list):
            for item in block:
                if isinstance(item, str):
                    add(item)
                elif isinstance(item, dict):
                    add(item.get("url") or item.get("src") or item.get("href"))
        elif isinstance(block, str):
            add(block)
    return urls


def merge_context_images(product: ProductContext, context: dict[str, Any]) -> ProductContext:
    """Apply WB Tool product context images onto ProductContext (context URLs win)."""
    from_context = _product_images_from_context(context)
    if not from_context:
        return product
    existing_urls = {img.url for img in product.images if img.url}
    existing_assets = {img.asset_id for img in product.images if img.asset_id}
    merged = list(product.images)
    for image in from_context:
        if image.url and image.url in existing_urls:
            continue
        if image.asset_id and image.asset_id in existing_assets:
            continue
        merged.append(image)
    product.images = merged
    return product


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
            response = await client.get(
                f"{self.base_url}/api/ai/products/{sku_id}/context",
                params=params,
            )
        response.raise_for_status()
        return response.json()

    async def product_media_urls(self, sku_id: int, account_id: int | None = None) -> list[str]:
        """Probe optional WB Tool media endpoints when /context has images: []."""
        params: dict[str, Any] = {}
        if account_id:
            params["account_id"] = account_id
        async with httpx.AsyncClient(timeout=20.0) as client:
            for segment in _PRODUCT_IMAGE_PATHS:
                url = f"{self.base_url}/api/ai/products/{sku_id}/{segment}"
                try:
                    response = await client.get(url, params=params)
                except httpx.HTTPError:
                    continue
                if response.status_code == 404:
                    continue
                if response.status_code >= 400:
                    logger.debug(
                        "wb tool media endpoint unavailable",
                        extra={"path": segment, "status": response.status_code},
                    )
                    continue
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    continue
                urls = _urls_from_media_payload(data)
                if urls:
                    return urls
        return []

    async def resolve_product_images(
        self,
        sku_id: int,
        account_id: int | None,
        product: ProductContext,
    ) -> tuple[ProductContext, list[str]]:
        """Merge images from context, optional WB Tool media APIs, then CDN fallback."""
        notes: list[str] = []
        context = await self.product_context(sku_id, account_id)
        product = ProductContext.model_validate({**product.model_dump(), **context})
        product = merge_context_images(product, context)

        has_urls = any(img.url for img in product.images)
        has_assets = any(img.asset_id for img in product.images)
        if not has_urls and not has_assets:
            media_urls = await self.product_media_urls(sku_id, account_id)
            if media_urls:
                product.images = [
                    ProductImage(url=url, role="source", alt=f"WB photo {index}")
                    for index, url in enumerate(media_urls, start=1)
                ]
                notes.append(f"Фото получены из WB Tool ({len(media_urls)} шт.).")
            elif product.nm_id and product.nm_id > 0:
                from identika.services.wb_cdn import wb_product_image_urls

                product.images = [
                    ProductImage(url=url, role="source", alt=f"WB CDN {index}")
                    for index, url in enumerate(wb_product_image_urls(product.nm_id), start=1)
                ]
                notes.append(
                    "В контексте WB Tool нет фото — используем CDN Wildberries. "
                    "При ошибке загрузки добавьте фото вручную слева на странице создания."
                )
            else:
                notes.append(
                    "Фото товара недоступны (нет nmID и URL). Загрузите до 4 фото вручную перед генерацией."
                )
        return product, notes

    async def upload_job(self, job: JobRecord, public_base_url: str = "") -> dict[str, Any]:
        """Upload approved job media to WB Tool.

        Primary: POST /api/ai/jobs/{job_id}/upload
        Fallback (501): POST /api/ai/jobs/{job_id}/upload/staging with export_url + manifest
        """
        payload = build_upload_payload(job, public_base_url)
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.base_url}/api/ai/jobs/{job.id}/upload",
                json=payload,
            )
            if response.status_code == 501:
                staging = await self._post_staging(client, job.id, payload)
                if staging.get("ok"):
                    return staging
                detail = _response_detail(response)
                return {
                    "ok": False,
                    "staging": True,
                    "status": 501,
                    "detail": detail,
                    "export_url": payload.get("export_url"),
                    "manifest_url": payload.get("manifest_url"),
                }
            if response.status_code >= 400:
                return {
                    "ok": False,
                    "status": response.status_code,
                    "detail": _response_detail(response),
                }
            data = response.json()
            if isinstance(data, dict) and "ok" not in data:
                data["ok"] = True
            return data

    async def _post_staging(
        self,
        client: httpx.AsyncClient,
        job_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        path = STAGING_UPLOAD_PATH.format(job_id=job_id)
        try:
            response = await client.post(f"{self.base_url}{path}", json=payload)
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": str(exc)}
        if response.status_code == 404:
            return {"ok": False, "status": 404}
        if response.status_code >= 400:
            return {"ok": False, "status": response.status_code, "detail": _response_detail(response)}
        data = response.json()
        if isinstance(data, dict):
            data.setdefault("ok", True)
            data.setdefault("staging", True)
            return data
        return {"ok": True, "staging": True}


def _response_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("detail") or body.get("message") or body
            if isinstance(detail, list):
                return json.dumps(detail, ensure_ascii=False)[:500]
            return str(detail)[:500]
    except (json.JSONDecodeError, ValueError):
        pass
    return (response.text or response.reason_phrase or "unknown error")[:500]


def upload_redirect_query(result: dict[str, Any]) -> str:
    """Build query string for job page upload feedback."""
    if result.get("ok"):
        return "upload=ok"
    if result.get("staging"):
        params = "upload=staging"
        detail = result.get("detail")
        if detail:
            params += f"&upload_detail={quote(str(detail)[:240])}"
        return params
    params = "upload=error"
    detail = result.get("detail")
    if detail:
        params += f"&upload_detail={quote(str(detail)[:240])}"
    status = result.get("status")
    if status:
        params += f"&upload_status_code={status}"
    return params
