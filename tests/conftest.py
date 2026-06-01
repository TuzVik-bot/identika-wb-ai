from __future__ import annotations

import pytest

from identika.models import ProductImage


def _png_bytes() -> bytes:
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "530000000a49444154789c6260000000020001e221bc330000000049454e44ae426082"
    )


@pytest.fixture(autouse=True)
def inject_product_photo_when_download_empty(monkeypatch, request) -> None:
    """Keep legacy tests working when WB CDN is unreachable in CI."""
    if request.node.get_closest_marker("no_photo_inject"):
        return

    import identika.services.jobs as jobs_mod

    real_download = jobs_mod.download_product_images

    async def _download(job_id: str, product, storage):
        product, warnings = await real_download(job_id, product, storage)
        from identika.services.product_images import has_source_assets

        if has_source_assets(product):
            return product, warnings
        asset_id = storage.add_asset(job_id, "test_product.png", _png_bytes(), "image/png")
        product.images = [ProductImage(asset_id=asset_id, role="source", alt="test")]
        return product, warnings

    monkeypatch.setattr(jobs_mod, "download_product_images", _download)
