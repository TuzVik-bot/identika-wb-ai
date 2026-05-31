from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from identika.api.routes import router
from identika.config import settings
from identika.middleware import ApiKeyMiddleware, UiBasicAuthMiddleware
from identika.services.jobs import JobService


def create_app() -> FastAPI:
    app = FastAPI(title="Identika WB AI", version="0.1.0")
    settings.identika_assets_dir.mkdir(parents=True, exist_ok=True)
    app.state.jobs = JobService()
    base = Path(__file__).parent
    app.state.templates = Jinja2Templates(directory=str(base / "templates"))
    app.mount("/static", StaticFiles(directory=str(base / "static")), name="static")
    app.add_middleware(UiBasicAuthMiddleware)
    app.add_middleware(ApiKeyMiddleware)
    app.include_router(router)
    return app


app = create_app()
