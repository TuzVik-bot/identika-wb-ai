"""Official Wildberries Content API photo source (fallback when WB Tool has no images)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from identika.config import settings

logger = logging.getLogger("identika.wb_content")

CARDS_LIST_PATH = "/content/v2/get/cards/list"
MEDIA_SAVE_PATH = "/content/v3/media/save"

# Photo size keys ordered from best to acceptable quality.
_PHOTO_SIZE_KEYS = ("big", "hq", "c516x688")

# WB media/save accepts at most 30 photos per card.
MAX_MEDIA_PHOTOS = 30


def _photo_url(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if not isinstance(entry, dict):
        return ""
    for key in _PHOTO_SIZE_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in entry.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pick_card(cards: list[Any], nm_id: int) -> dict[str, Any] | None:
    """Exact nmID match wins; a card without nmID is the only accepted fallback."""
    fallback: dict[str, Any] | None = None
    for card in cards:
        if not isinstance(card, dict):
            continue
        card_nm_id = card.get("nmID")
        if card_nm_id == nm_id:
            return card
        if card_nm_id is None and fallback is None:
            fallback = card
    return fallback


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("detail") or body.get("message") or body
            return str(detail)[:300]
    except (ValueError, TypeError):
        pass
    return (response.text or response.reason_phrase or "unknown error")[:300]


def photo_urls_from_card(card: dict[str, Any]) -> list[str]:
    """Extract deduped photo URLs from a WB Content API card, best quality first."""
    urls: list[str] = []
    seen: set[str] = set()
    for entry in card.get("photos") or []:
        url = _photo_url(entry)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


class WBContentClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or settings.wb_content_base_url).rstrip("/")
        self.token = (token if token is not None else settings.wb_content_api_token).strip()

    async def _fetch_card(self, nm_id: int) -> dict[str, Any] | None:
        if not self.token or nm_id <= 0:
            return None
        # The API ignores textSearch/nmIDs filters on /content/v2/get/cards/list, so
        # scan pages (keyset cursor) and match nmID client-side.
        try:
            async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
                cursor: dict[str, Any] = {"limit": 100}
                fallback: dict[str, Any] | None = None
                for _ in range(5):
                    payload = {
                        "settings": {"cursor": cursor, "filter": {"withPhoto": -1}},
                    }
                    response = await client.post(
                        f"{self.base_url}{CARDS_LIST_PATH}",
                        headers={
                            "Authorization": self.token,
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                    cards = data.get("cards") if isinstance(data, dict) else None
                    if isinstance(cards, list):
                        for card in cards:
                            if not isinstance(card, dict):
                                continue
                            card_nm_id = card.get("nmID")
                            if card_nm_id == nm_id:
                                return card
                            if card_nm_id is None and fallback is None:
                                fallback = card
                    if not isinstance(cards, list) or not cards:
                        break
                    next_cursor = data.get("cursor") if isinstance(data, dict) else None
                    if not isinstance(next_cursor, dict):
                        break
                    next_updated = next_cursor.get("updatedAt")
                    next_nm_id = next_cursor.get("nmID")
                    if not next_updated or not next_nm_id:
                        break
                    cursor = {"limit": 100, "updatedAt": next_updated, "nmID": next_nm_id}
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "wb content api request failed",
                extra={"nm_id": nm_id, "error": type(exc).__name__},
            )
            return None
        if fallback is not None:
            return fallback
        logger.debug("wb content api returned no cards", extra={"nm_id": nm_id})
        return None

    async def product_card(self, nm_id: int) -> dict[str, Any] | None:
        """Return the raw WB Content API card for nm_id (title, photos, characteristics); None if unavailable."""
        card = await self._fetch_card(nm_id)
        if card is None:
            return None
        # A fallback card without an exact nmID match may belong to another product.
        if card.get("nmID") != nm_id:
            return None
        return card

    async def product_photo_urls(self, nm_id: int) -> list[str]:
        """Return WB Content API photo URLs for nm_id; never raises."""
        card = await self._fetch_card(nm_id)
        if card is None:
            return []
        urls: list[str] = []
        seen: set[str] = set()
        for entry in card.get("photos") or []:
            url = _photo_url(entry)
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
        return urls

    async def upload_media(self, nm_id: int, photo_urls: list[str]) -> dict[str, Any]:
        """Attach photos to a WB card by public URL via official Content API.

        Returns {"ok": True, "upload_id": ...} on success or {"ok": False, ...} on failure.
        """
        clean_urls = [url.strip() for url in photo_urls if url.strip()][:MAX_MEDIA_PHOTOS]
        if not self.token:
            return {"ok": False, "reason": "no_token"}
        if nm_id <= 0:
            return {"ok": False, "reason": "no_nm_id"}
        if not clean_urls:
            return {"ok": False, "reason": "no_photos"}
        payload = {
            "nmID": nm_id,
            "data": [{"photo": index, "url": url} for index, url in enumerate(clean_urls, start=1)],
        }
        try:
            async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
                response = await client.post(
                    f"{self.base_url}{MEDIA_SAVE_PATH}",
                    headers={
                        "Authorization": self.token,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "wb content media save failed",
                extra={"nm_id": nm_id, "error": type(exc).__name__},
            )
            return {"ok": False, "reason": "network", "detail": type(exc).__name__}
        if response.status_code >= 400:
            detail = _error_detail(response)
            logger.warning(
                "wb content media save rejected",
                extra={"nm_id": nm_id, "status": response.status_code},
            )
            return {
                "ok": False,
                "reason": "http",
                "status": response.status_code,
                "detail": detail,
            }
        try:
            data = response.json()
        except ValueError:
            data = {}
        upload_id = data.get("id") if isinstance(data, dict) else None
        return {"ok": True, "upload_id": upload_id}
