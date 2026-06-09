from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from identika.api.routes import router
from identika.config import settings
from identika.middleware import ApiKeyMiddleware, UiBasicAuthMiddleware
from identika.services.jobs import JobService
from identika.ui_labels import job_status_label, short_datetime


def create_app() -> FastAPI:
    app = FastAPI(title="Identika WB AI", version="0.1.0")
    settings.identika_assets_dir.mkdir(parents=True, exist_ok=True)
    app.state.jobs = JobService()
    base = Path(__file__).parent
    app.state.templates = Jinja2Templates(directory=str(base / "templates"))
    static_dir = str(base / "static")
    # Cache-busting token derived from the stylesheet mtime so that upstream
    # proxies/CDNs (which may cache a stale 401/200 for the bare asset URL)
    # fetch a fresh copy on every deploy.
    css_path = base / "static" / "app.css"
    try:
        static_version = str(int(css_path.stat().st_mtime))
    except OSError:
        static_version = "0"
    app.state.templates.env.globals["static_version"] = static_version
    app.state.templates.env.filters["status_label"] = job_status_label
    app.state.templates.env.filters["short_datetime"] = short_datetime
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    if settings.public_base_path:
        app.mount(
            settings.static_url_prefix,
            StaticFiles(directory=static_dir),
            name="static_prefixed",
        )
    app.add_middleware(UiBasicAuthMiddleware)
    app.add_middleware(ApiKeyMiddleware)
    app.include_router(router)
    return app


app = create_app()
