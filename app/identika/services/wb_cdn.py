from __future__ import annotations

"""Wildberries CDN image URL helpers (fallback when WB Tool returns images: [])."""

import httpx

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
    (2621, "16"),
    (2837, "17"),
    (3053, "18"),
    (3269, "19"),
    (3485, "20"),
)

_PROBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; IdentikaWB/1.0; +https://eurasia-transline.online/identika/)"
    ),
    "Accept": "image/*,*/*;q=0.8",
}


def wb_basket_id(vol: int) -> str:
    for upper, basket in _BASKET_RANGES:
        if vol <= upper:
            return basket
    return "21"


def wb_image_url_candidates(nm_id: int, index: int = 1) -> list[str]:
    """Return candidate WB CDN URLs for a product photo (newest hosts/formats first)."""
    vol = nm_id // 100_000
    part = nm_id // 1_000
    basket = wb_basket_id(vol)
    hosts = (
        f"basket-{basket}.wbbasket.ru",
        f"basket-{basket}.wb.ru",
        f"basket-{basket}.wbstatic.net",
    )
    urls: list[str] = []
    for host in hosts:
        for ext in ("webp", "jpg", "jpeg"):
            urls.append(f"https://{host}/vol{vol}/part{part}/{nm_id}/images/big/{index}.{ext}")
    return urls


def wb_product_image_urls(nm_id: int, max_pics: int = 5) -> list[str]:
    """Primary CDN URL per photo index (first candidate per index)."""
    if nm_id <= 0:
        return []
    return [wb_image_url_candidates(nm_id, index)[0] for index in range(1, max_pics + 1)]


async def url_exists(client: httpx.AsyncClient, url: str) -> bool:
    try:
        if hasattr(client, "head"):
            response = await client.head(url)
        else:
            response = await client.get(url)
        if response.status_code == 404:
            return False
        if response.status_code in (405, 501) and hasattr(client, "get"):
            response = await client.get(url)
        if response.status_code == 404:
            return False
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "").lower()
        if content_type.startswith("image/"):
            return True
        # Some CDNs omit content-type on HEAD; accept small binary bodies.
        if response.status_code == 200 and int(response.headers.get("content-length", "1")) > 64:
            return True
    except httpx.HTTPError:
        return False
    return False


async def discover_wb_image_urls(
    nm_id: int,
    *,
    max_index: int = 10,
    client: httpx.AsyncClient | None = None,
) -> list[str]:
    """Probe CDN indices 1..max_index and return URLs that respond with an image."""
    if nm_id <= 0:
        return []

    async def _probe(active_client: httpx.AsyncClient) -> list[str]:
        found: list[str] = []
        misses = 0
        for index in range(1, max_index + 1):
            candidates = wb_image_url_candidates(nm_id, index)
            hit = False
            for candidate in candidates:
                if await url_exists(active_client, candidate):
                    found.append(candidate)
                    hit = True
                    misses = 0
                    break
            if not hit:
                misses += 1
                if misses >= 2 and found:
                    break
        return found

    if client is not None:
        return await _probe(client)

    async with httpx.AsyncClient(
        timeout=12.0,
        follow_redirects=True,
        headers=_PROBE_HEADERS,
    ) as owned:
        return await _probe(owned)
