from __future__ import annotations

import logging

import httpx

from identika.models import CreateJobRequest, ProductContext, ProductImage
from identika.services.wb_cdn import wb_image_url_candidates
from identika.storage import Storage

logger = logging.getLogger("identika.product_images")

MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; IdentikaWB/1.0; +https://eurasia-transline.online/identika/)"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
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


def _has_source_assets(product: ProductContext) -> bool:
    return any(img.role == "source" and img.asset_id for img in product.images)


def _pending_urls(product: ProductContext) -> list[str]:
    return [img.url for img in product.images if img.url and not img.asset_id]


def ensure_product_image_urls(product: ProductContext, max_pics: int = 5) -> ProductContext:
    """Fill product.images from WB CDN when WB Tool context has no photo URLs."""
    if _has_source_assets(product) or _pending_urls(product):
        return product
    nm_id = product.nm_id
    if not nm_id or nm_id <= 0:
        return product
    images = list(product.images)
    for index in range(1, max_pics + 1):
        images.append(
            ProductImage(
                url=wb_image_url_candidates(nm_id, index)[0],
                role="source",
                alt=f"WB photo {index}",
            )
        )
    product.images = images
    return product


def _detect_image_type(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif", ".gif"
    return None


def _resolve_content_type(raw_header: str, data: bytes) -> tuple[str, str] | None:
    content_type = raw_header.split(";")[0].strip().lower()
    suffix = ALLOWED_CONTENT_TYPES.get(content_type)
    if suffix:
        return content_type, suffix
    return _detect_image_type(data)


async def _download_image_bytes(client: httpx.AsyncClient, url: str) -> tuple[bytes, str] | None:
    candidates = [url]
    if "/images/big/" in url:
        base = url.rsplit(".", 1)[0]
        ext = url.rsplit(".", 1)[-1].lower() if "." in url else ""
        if ext == "webp":
            candidates.append(f"{base}.jpg")
        elif ext == "jpg":
            candidates.append(f"{base}.webp")
        if "wbbasket.ru" in url:
            candidates.append(url.replace("wbbasket.ru", "wb.ru"))
        elif "wb.ru" in url and "wbbasket.ru" not in url:
            candidates.append(url.replace("wb.ru", "wbbasket.ru"))
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            response = await client.get(candidate)
            if response.status_code == 404:
                continue
            response.raise_for_status()
            data = response.content
            if not data or len(data) > MAX_IMAGE_BYTES:
                continue
            resolved = _resolve_content_type(response.headers.get("content-type", ""), data)
            if not resolved:
                continue
            content_type, _suffix = resolved
            return data, content_type
        except httpx.HTTPError:
            continue
    return None


async def download_product_images(
    job_id: str,
    product: ProductContext,
    storage: Storage,
) -> tuple[ProductContext, list[str]]:
    had_explicit_urls = bool(_pending_urls(product))
    existing_sources = [
        img for img in product.images if img.role == "source" and img.asset_id
    ]
    needs_cdn_fallback = (
        not existing_sources
        and not had_explicit_urls
        and bool(product.nm_id and product.nm_id > 0)
    )
    product = ensure_product_image_urls(product)
    images = list(product.images)
    warnings: list[str] = []
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers=DOWNLOAD_HEADERS,
    ) as client:
        for idx, image in enumerate(images):
            if image.asset_id or not image.url:
                continue
            nm_index = None
            if product.nm_id and "/images/big/" in image.url:
                try:
                    nm_index = int(image.url.rsplit("/", 1)[-1].split(".", 1)[0])
                except ValueError:
                    nm_index = None
            download_targets = [image.url]
            if nm_index and product.nm_id:
                download_targets = wb_image_url_candidates(product.nm_id, nm_index)
            downloaded: tuple[bytes, str] | None = None
            for target in download_targets:
                downloaded = await _download_image_bytes(client, target)
                if downloaded:
                    break
            if not downloaded:
                logger.warning(
                    "product image download failed",
                    extra={"url": image.url, "nm_id": product.nm_id},
                )
                if had_explicit_urls:
                    warnings.append(f"Не удалось скачать фото товара ({image.url}).")
                continue
            data, content_type = downloaded
            suffix = ALLOWED_CONTENT_TYPES.get(content_type, ".jpg")
            asset_id = storage.add_asset(job_id, f"product_{idx:02d}{suffix}", data, content_type)
            image.asset_id = asset_id
            if not image.role:
                image.role = "source"
    downloaded_images = [img for img in images if img.asset_id and img.role == "source"]
    if downloaded_images:
        product.images = downloaded_images
    elif existing_sources:
        product.images = existing_sources
    elif not needs_cdn_fallback:
        product.images = images
    else:
        product.images = []
    source_assets = [img for img in product.images if img.role == "source" and img.asset_id]
    if not source_assets and had_explicit_urls:
        warnings.append(
            "Фото товара WB недоступны — слайды будут без исходного фото (проверьте URL или загрузите фото вручную)."
        )
    elif not source_assets and needs_cdn_fallback:
        warnings.append(
            f"Фото товара WB (nmID {product.nm_id}) недоступны на CDN — загрузите фото вручную или включите AI-фон."
        )
    elif not source_assets:
        warnings.append(
            "В карточке WB нет фото — слайды будут без исходного фото (загрузите фото вручную или включите AI-фон)."
        )
    return product, warnings
