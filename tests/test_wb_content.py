from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from identika.services.wb_content import WBContentClient


class FakeClient:
    """Minimal httpx.AsyncClient stand-in for WB Content API calls."""

    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, headers: dict[str, str] | None = None, json: Any = None):
        self.calls.append({"url": url, "headers": headers or {}, "json": json})
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _json_response(payload: Any, status: int = 200) -> httpx.Response:
    request = httpx.Request("POST", "https://content-api.wildberries.ru/content/v2/get/cards/list")
    return httpx.Response(status, json=payload, request=request)


@pytest.fixture()
def patch_client(monkeypatch):
    def _install(fake: FakeClient) -> FakeClient:
        monkeypatch.setattr(
            "identika.services.wb_content.httpx.AsyncClient",
            lambda *a, **k: fake,
        )
        return fake

    return _install


def _client() -> WBContentClient:
    return WBContentClient(base_url="https://content-api.wildberries.ru", token="token-123")


def test_product_photo_urls_prefers_big_size(patch_client) -> None:
    fake = patch_client(
        FakeClient(
            _json_response(
                {
                    "cards": [
                        {
                            "nmID": 4242,
                            "photos": [
                                {"big": "https://cdn/1-big.jpg", "c516x688": "https://cdn/1-sm.jpg"},
                                {"hq": "https://cdn/2-hq.jpg"},
                                {"c516x688": "https://cdn/3-sm.jpg"},
                            ],
                        }
                    ]
                }
            )
        )
    )
    urls = asyncio.run(_client().product_photo_urls(4242))
    assert urls == [
        "https://cdn/1-big.jpg",
        "https://cdn/2-hq.jpg",
        "https://cdn/3-sm.jpg",
    ]
    call = fake.calls[0]
    assert call["url"].endswith("/content/v2/get/cards/list")
    assert call["headers"]["Authorization"] == "token-123"
    assert call["json"]["settings"]["filter"]["textSearch"] == "4242"
    assert call["json"]["settings"]["cursor"]["limit"] == 1


def test_product_photo_urls_accepts_plain_strings_and_dedupes(patch_client) -> None:
    patch_client(
        FakeClient(
            _json_response(
                {
                    "cards": [
                        {
                            "nmID": 7,
                            "photos": [
                                "https://cdn/a.jpg",
                                "https://cdn/a.jpg",
                                {"unknown_size": "https://cdn/b.jpg"},
                                123,
                            ],
                        }
                    ]
                }
            )
        )
    )
    assert asyncio.run(_client().product_photo_urls(7)) == [
        "https://cdn/a.jpg",
        "https://cdn/b.jpg",
    ]


def test_product_photo_urls_ignores_card_with_other_nm_id(patch_client) -> None:
    patch_client(
        FakeClient(
            _json_response(
                {"cards": [{"nmID": 999, "photos": [{"big": "https://cdn/other.jpg"}]}]}
            )
        )
    )
    assert asyncio.run(_client().product_photo_urls(4242)) == []


def test_product_photo_urls_matches_exact_card_among_many(patch_client) -> None:
    patch_client(
        FakeClient(
            _json_response(
                {
                    "cards": [
                        {"nmID": 111, "photos": [{"big": "https://cdn/wrong.jpg"}]},
                        {"nmID": 4242, "photos": [{"big": "https://cdn/right.jpg"}]},
                    ]
                }
            )
        )
    )
    assert asyncio.run(_client().product_photo_urls(4242)) == ["https://cdn/right.jpg"]


def test_product_photo_urls_http_error_returns_empty(patch_client) -> None:
    patch_client(FakeClient(_json_response({"detail": "unauthorized"}, status=401)))
    assert asyncio.run(_client().product_photo_urls(4242)) == []


def test_product_photo_urls_network_error_returns_empty(patch_client) -> None:
    patch_client(FakeClient(error=httpx.ConnectError("dns")))
    assert asyncio.run(_client().product_photo_urls(4242)) == []


def test_product_photo_urls_without_token_skips_network(patch_client) -> None:
    fake = patch_client(FakeClient(_json_response({"cards": []})))
    client = WBContentClient(base_url="https://content-api.wildberries.ru", token="")
    assert asyncio.run(client.product_photo_urls(4242)) == []
    assert fake.calls == []


def test_product_photo_urls_without_nm_id_skips_network(patch_client) -> None:
    fake = patch_client(FakeClient(_json_response({"cards": []})))
    assert asyncio.run(_client().product_photo_urls(0)) == []
    assert fake.calls == []
