from __future__ import annotations

"""Wildberries CDN image URL helpers (fallback when WB Tool returns images: [])."""

_BASKET_RANGES: tuple[tuple[int, str], ...] = (
    (143, "01"),
    (287, "02"),
    (431, "03"),
    (719, "04"),
    (1007, "05"),
    (1061, "06"),
    (1115, "07"),
    (1169, "08"),
    (1313, "09"),
    (1601, "10"),
    (1655, "11"),
    (1919, "12"),
    (2045, "13"),
    (2189, "14"),
    (2405, "15"),
)


def wb_basket_id(vol: int) -> str:
    for upper, basket in _BASKET_RANGES:
        if vol <= upper:
            return basket
    return "16"


def wb_image_url_candidates(nm_id: int, index: int = 1) -> list[str]:
    """Return candidate WB CDN URLs for a product photo (newest hosts/formats first)."""
    vol = nm_id // 100_000
    part = nm_id // 1_000
    basket = wb_basket_id(vol)
    hosts = (f"basket-{basket}.wbbasket.ru", f"basket-{basket}.wb.ru")
    urls: list[str] = []
    for host in hosts:
        for ext in ("webp", "jpg"):
            urls.append(f"https://{host}/vol{vol}/part{part}/{nm_id}/images/big/{index}.{ext}")
    return urls


def wb_product_image_urls(nm_id: int, max_pics: int = 5) -> list[str]:
    """Primary CDN URL per photo index (first candidate per index)."""
    if nm_id <= 0:
        return []
    return [wb_image_url_candidates(nm_id, index)[0] for index in range(1, max_pics + 1)]
