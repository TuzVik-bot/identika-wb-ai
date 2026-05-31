from __future__ import annotations

import base64
import secrets

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from identika.config import settings


class ApiKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        if not settings.identika_api_key:
            return await call_next(request)
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)
        header_key = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix(
            "Bearer "
        ).strip()
        if header_key != settings.identika_api_key:
            return JSONResponse(status_code=401, content={"detail": "invalid or missing API key"})
        return await call_next(request)


class UiBasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        if not settings.identika_ui_password:
            return await call_next(request)
        path = request.url.path
        if path.startswith("/v1/") or path.startswith("/static/") or path == "/health":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                _, password = decoded.split(":", 1)
                if secrets.compare_digest(password, settings.identika_ui_password):
                    return await call_next(request)
            except (ValueError, UnicodeDecodeError):
                pass
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Identika"'},
            content="Authentication required",
            media_type="text/plain",
        )
