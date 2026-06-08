from __future__ import annotations

import logging

import httpx

from identika.models import CreateJobRequest, ProductContext, ProductImage
from identika.services.wb_cdn import discover_wb_image_urls, wb_image_url_candidates, wb_product_image_urls
from identika.storage import Storage

logger = logging.getLogger("identika.product_images")

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MIN_PRODUCT_IMAGE_BYTES = 64
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


class SourcePhotosRequiredError(ValueError):
    """Generation blocked because no product photos are available."""


SOURCE_PHOTOS_REQUIRED_MSG = (
    "Нет фото товара. Загрузите хотя бы одно фото в блоке «Ваш товар» "
    "или отметьте «Сгенерировать без фото»."
)

SOURCE_PHOTOS_AFTER_DOWNLOAD_MSG = (
    "Фото товара не загружены: WB/CDN недоступны. "
    "Загрузите фото вручную на странице проекта и нажмите «Пересобрать слайды»."
)


def count_source_assets(product: ProductContext) -> int:
    return sum(1 for img in product.images if img.role == "source" and img.asset_id)


def has_source_assets(product: ProductContext) -> bool:
    return count_source_assets(product) > 0


def _has_source_assets(product: ProductContext) -> bool:
    return has_source_assets(product)


def validate_can_start_generation(
    product: ProductContext,
    *,
    allow_without_photos: bool = False,
) -> None:
    if allow_without_photos or has_source_assets(product):
        return
    if _pending_urls(product):
        return
    if product.nm_id and product.nm_id > 0:
        return
    raise SourcePhotosRequiredError(SOURCE_PHOTOS_REQUIRED_MSG)


def ensure_source_assets_after_download(
    product: ProductContext,
    *,
    allow_without_photos: bool = False,
) -> None:
    if allow_without_photos or has_source_assets(product):
        return
    raise SourcePhotosRequiredError(SOURCE_PHOTOS_AFTER_DOWNLOAD_MSG)


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
    for index, url in enumerate(wb_product_image_urls(nm_id, max_pics=max_pics), start=1):
        images.append(
            ProductImage(
                url=url,
                role="source",
                alt=f"WB photo {index}",
            )
        )
    product.images = images
    return product


async def ensure_product_image_urls_async(
    product: ProductContext,
    *,
    max_pics: int = 10,
    client: httpx.AsyncClient | None = None,
) -> ProductContext:
    """CDN fallback with HEAD probe so only existing photo indices are used."""
    if _has_source_assets(product) or _pending_urls(product):
        return product
    nm_id = product.nm_id
    if not nm_id or nm_id <= 0:
        return product
    discovered = await discover_wb_image_urls(nm_id, max_index=max_pics, client=client)
    urls = discovered or wb_product_image_urls(nm_id, max_pics=min(max_pics, 5))
    product.images = [
        ProductImage(url=url, role="source", alt=f"WB photo {index}")
        for index, url in enumerate(urls, start=1)
    ]
    return product


def _source_image_sort_key(image: ProductImage) -> tuple[int, int]:
    if image.url and "/images/big/" in image.url:
        try:
            return (0, int(image.url.rsplit("/", 1)[-1].split(".", 1)[0]))
        except ValueError:
            pass
    return (1, 0)


def _is_valid_product_image(data: bytes) -> bool:
    if not data or len(data) < MIN_PRODUCT_IMAGE_BYTES or len(data) > MAX_IMAGE_BYTES:
        return False
    return _detect_image_type(data) is not None


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
        
        # Build host variants
        hosts = []
        if "wbbasket.ru" in url:
            hosts.append(url.replace("wbbasket.ru", "wb.ru"))
            hosts.append(url.replace("wbbasket.ru", "wbstatic.net"))
        elif "wb.ru" in url and "wbbasket.ru" not in url:
            hosts.append(url.replace("wb.ru", "wbbasket.ru"))
            hosts.append(url.replace("wb.ru", "wbstatic.net"))
        elif "wbstatic.net" in url:
            hosts.append(url.replace("wbstatic.net", "wbbasket.ru"))
            hosts.append(url.replace("wbstatic.net", "wb.ru"))
            
        candidates.extend(hosts)
        
        # Add extension variants for all hosts
        ext_variants = []
        for c in candidates:
            c_base = c.rsplit(".", 1)[0]
            if ext == "webp":
                ext_variants.append(f"{c_base}.jpg")
            elif ext == "jpg":
                ext_variants.append(f"{c_base}.webp")
        candidates.extend(ext_variants)
        
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
            if not _is_valid_product_image(data):
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
    if not needs_cdn_fallback:
        product = ensure_product_image_urls(product)
    images = list(product.images)
    warnings: list[str] = []
    failed_downloads = 0
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers=DOWNLOAD_HEADERS,
        trust_env=False,
    ) as client:
        if needs_cdn_fallback:
            product = await ensure_product_image_urls_async(product, client=client)
            images = list(product.images)
            if not images:
                warnings.append(
                    f"CDN Wildberries недоступен для nmID {product.nm_id}: ни один photo endpoint не ответил изображением."
                )
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
                candidates = wb_image_url_candidates(product.nm_id, nm_index)
                if image.url not in candidates:
                    download_targets.extend(candidates)
                else:
                    download_targets = candidates
            downloaded: tuple[bytes, str] | None = None
            for target in download_targets:
                downloaded = await _download_image_bytes(client, target)
                if downloaded:
                    break
            if not downloaded:
                failed_downloads += 1
                logger.warning(
                    "product image download failed",
                    extra={"url": image.url, "nm_id": product.nm_id},
                )
                if had_explicit_urls:
                    warnings.append(f"Фото WB недоступно по URL: {image.url}")
                continue
            data, content_type = downloaded
            suffix = ALLOWED_CONTENT_TYPES.get(content_type, ".jpg")
            asset_id = storage.add_asset(job_id, f"product_{idx:02d}{suffix}", data, content_type)
            image.asset_id = asset_id
            if not image.role:
                image.role = "source"
    downloaded_images = [img for img in images if img.asset_id and img.role == "source"]
    if downloaded_images:
        downloaded_images.sort(key=_source_image_sort_key)
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
            "Фото товара WB недоступны по переданным URL — слайды будут без исходного фото (проверьте URL или загрузите фото вручную)."
        )
    elif not source_assets and needs_cdn_fallback:
        warnings.append(
            f"Фото товара WB (nmID {product.nm_id}) недоступны на CDN — загрузите фото вручную."
        )
    elif not source_assets:
        warnings.append(
            "В карточке WB нет фото — загрузите пример фото товара вручную перед экспортом."
        )
    elif failed_downloads:
        source_count = len(source_assets)
        warnings.append(
            f"Часть фото WB недоступна на CDN: успешно скачано {source_count}, пропущено {failed_downloads}."
        )
    return product, warnings
