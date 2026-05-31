from __future__ import annotations

from identika.models import CreateJobRequest, ProductContext, ProductImage


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
