from __future__ import annotations

import logging

import httpx

from identika.models import CreateJobRequest, ProductContext, ProductImage
from identika.storage import Storage

logger = logging.getLogger("identika.product_images")

MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def attach_source_images(product: ProductContext, asset_ids: list[str]) -> ProductContext:
    images = list(product.images)
    existing = {img.asset_id for img in images if img.asset_id}
    for asset_id in asset_ids:
        clean = asset_id.strip()
        if not clean or clean in existing:
            continue
        images.append(ProductImage(asset_id=clean, role="source"))
        existing.add(clean)
    product.images = images
    return product


def prepare_job_request(payload: CreateJobRequest) -> CreateJobRequest:
    if payload.source_image_asset_ids:
        attach_source_images(payload.product, payload.source_image_asset_ids)
    return payload


async def download_product_images(
    job_id: str,
    product: ProductContext,
    storage: Storage,
) -> ProductContext:
    images = list(product.images)
    changed = False
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for idx, image in enumerate(images):
            if image.asset_id or not image.url:
                continue
            try:
                response = await client.get(image.url)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip().lower()
                suffix = ALLOWED_CONTENT_TYPES.get(content_type)
                if not suffix:
                    continue
                data = response.content
                if not data or len(data) > MAX_IMAGE_BYTES:
                    continue
                asset_id = storage.add_asset(job_id, f"product_{idx:02d}{suffix}", data, content_type)
                image.asset_id = asset_id
                if not image.role:
                    image.role = "source"
                changed = True
            except (httpx.HTTPError, ValueError, OSError) as exc:
                logger.warning("product image download failed", extra={"url": image.url, "error": str(exc)})
                continue
    if changed:
        product.images = images
    return product
