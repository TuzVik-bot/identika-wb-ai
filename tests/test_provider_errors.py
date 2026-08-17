from __future__ import annotations

import httpx

from identika.providers.errors import describe_openrouter_error


def _status_error(status: int, body: str = "boom") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


def test_describe_bad_api_key() -> None:
    for status in (401, 403):
        message = describe_openrouter_error(_status_error(status))
        assert "API-ключ" in message
        assert f"HTTP {status}" in message


def test_describe_missing_credits() -> None:
    message = describe_openrouter_error(_status_error(402))
    assert "кредитов" in message
    assert "openrouter.ai/settings/credits" in message


def test_describe_rate_limit() -> None:
    message = describe_openrouter_error(_status_error(429))
    assert "HTTP 429" in message
    assert "позже" in message


def test_describe_bad_model() -> None:
    message = describe_openrouter_error(_status_error(404, "no such model"))
    assert "HTTP 404" in message
    assert "no such model" in message


def test_describe_generic_http_status() -> None:
    message = describe_openrouter_error(_status_error(500, "server exploded"))
    assert "HTTP 500" in message
    assert "server exploded" in message


def test_describe_network_error() -> None:
    message = describe_openrouter_error(httpx.ConnectError("dns failure"))
    assert "сеть недоступна" in message
    assert "ConnectError" in message


def test_describe_non_httpx_error() -> None:
    assert describe_openrouter_error(ValueError("bad json")) == "ValueError"
