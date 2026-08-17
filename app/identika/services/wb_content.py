"""Official Wildberries Content API photo source (fallback when WB Tool has no images)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from identika.config import settings

logger = logging.getLogger("identika.wb_content")

CARDS_LIST_PATH = "/content/v2/get/cards/list"

# Photo size keys ordered from best to acceptable quality.
_PHOTO_SIZE_KEYS = ("big", "hq", "c516x688")


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


class WBContentClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or settings.wb_content_base_url).rstrip("/")
        self.token = (token if token is not None else settings.wb_content_api_token).strip()

    async def product_photo_urls(self, nm_id: int) -> list[str]:
        """Return WB Content API photo URLs for nm_id; never raises."""
        if not self.token or nm_id <= 0:
            return []
        payload = {
            "settings": {
                "cursor": {"limit": 1},
                "filter": {"textSearch": str(nm_id), "withPhoto": -1},
            }
        }
        try:
            async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
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
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "wb content api request failed",
                extra={"nm_id": nm_id, "error": type(exc).__name__},
            )
            return []

        cards = data.get("cards") if isinstance(data, dict) else None
        if not isinstance(cards, list):
            logger.debug("wb content api returned no cards", extra={"nm_id": nm_id})
            return []
        card = _pick_card(cards, nm_id)
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
