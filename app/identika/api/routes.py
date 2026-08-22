from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from identika import __version__
from identika.config import EffectiveSettings, mask_api_key, settings
from identika.models import (
    BatchCreateJobsRequest,
    CreateJobRequest,
    ProductContext,
    ProductImage,
    ResultTextPatch,
    SlideTextUpdate,
)
from identika.services.category_templates import (
    delete_category_template,
    get_category_template,
    list_category_templates,
    list_category_template_views,
    save_category_template,
    template_from_form,
)
from identika.services.jobs import JobService
from identika.services.product_images import (
    SourcePhotosRequiredError,
    attach_source_images,
    attach_source_image_urls,
    count_source_assets,
    validate_source_image_urls,
    validate_can_start_generation,
)
from identika.services.uploads import save_source_images
from identika.services.wb_content import WBContentClient, photo_urls_from_card
from identika.services.wb_tool import WBToolClient, upload_redirect_query
from identika.ui_labels import job_status_label

router = APIRouter()

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}

PROJECT_STATUS_FILTERS = ("succeeded", "approved", "running", "failed")


def apply_no_cache(response: HTMLResponse | JSONResponse | FileResponse) -> HTMLResponse | JSONResponse | FileResponse:
    for key, value in NO_CACHE_HEADERS.items():
        response.headers[key] = value
    return response


def service(request: Request) -> JobService:
    return request.app.state.jobs


def url(path: str) -> str:
    path = "/" + path.lstrip("/")
    return f"{settings.public_base_path}{path}"


def public_base_url(request: Request) -> str:
    base = settings.public_base_path.rstrip("/")
    if request.url.hostname:
        scheme = request.url.scheme
        host = request.headers.get("host") or request.url.netloc
        return f"{scheme}://{host}{base}"
    return base


def parse_source_image_ids(*values: str | None) -> list[str]:
    ids: list[str] = []
    for value in values:
        if not value:
            continue
        for part in value.replace(" ", "").split(","):
            clean = part.strip()
            if clean and clean not in ids:
                ids.append(clean)
    return ids


def parse_source_image_urls(*values: str | None) -> list[str]:
    urls: list[str] = []
    for value in values:
        if not value:
            continue
        for part in value.replace(",", "\n").splitlines():
            clean = part.strip()
            if clean and clean not in urls:
                urls.append(clean)
    return validate_source_image_urls(urls)


def photo_error_redirect(
    account_id: int,
    brief: str,
    category_template_id: str,
    error: str,
) -> RedirectResponse:
    return RedirectResponse(
        url=url(
            f"/create?account_id={account_id}&brief={quote(brief)}"
            f"&category_template_id={quote(category_template_id.strip())}"
            f"&photo_error={quote(error)}"
        ),
        status_code=303,
    )


def source_photo_status(product: ProductContext) -> dict[str, str | int | bool]:
    source_count = count_source_assets(product)
    pending_urls = sum(
        1
        for image in product.images
        if image.role == "source" and image.url and not image.asset_id
    )
    if source_count:
        return {
            "state": "ok",
            "tone": "ok",
            "label": "Фото подключены",
            "count": source_count,
            "count_label": f"{source_count} фото",
            "description": "Слайды и экспорт используют исходные изображения товара.",
            "needs_action": False,
        }
    if pending_urls:
        return {
            "state": "pending_url",
            "tone": "warning",
            "label": "Фото не скачались",
            "count": 0,
            "count_label": "0 фото",
            "description": (
                f"Найдено URL фото: {pending_urls}, но файлы не сохранены. "
                "Загрузите фото вручную и пересоберите слайды."
            ),
            "needs_action": True,
        }
    return {
        "state": "missing",
        "tone": "warning",
        "label": "Фото отсутствуют",
        "count": 0,
        "count_label": "0 фото",
        "description": "Пакет собран без исходного фото товара. Добавьте реальные фото перед экспортом.",
        "needs_action": True,
    }


def result_readiness(result) -> dict[str, str | list[str]]:
    blockers: list[str] = []
    checks: list[str] = []
    source_count = count_source_assets(result.product)
    if source_count:
        checks.append(f"{source_count} фото товара подключено")
    else:
        blockers.append("Нужно фото товара")

    if len(result.slides) == 10 and all(slide.asset_id for slide in result.slides):
        checks.append("10 слайдов готовы")
    else:
        blockers.append("Нужно пересобрать 10 слайдов")

    if result.rich.blocks and result.rich.zip_asset_id:
        checks.append(f"{len(result.rich.blocks)} rich-блоков готовы")
    else:
        blockers.append("Нужно пересобрать Rich-пакет")

    if result.export_asset_id:
        checks.append("Export ZIP собран")
    else:
        blockers.append("Нужно собрать Export ZIP")

    if blockers:
        tone = "danger"
        label = blockers[0]
        summary = "Перед approve исправьте критичные пункты."
    elif result.warnings:
        tone = "warning"
        label = "Готово к approve"
        summary = "Можно утверждать, но проверьте предупреждения оператора."
    else:
        tone = "ok"
        label = "Готово к approve"
        summary = "Пакет готов к финализации и экспорту."

    return {
        "tone": tone,
        "label": label,
        "summary": summary,
        "checks": checks,
        "blockers": blockers,
    }


def dashboard_stats(jobs: list) -> dict[str, int]:
    generated = [job for job in jobs if job.status in {"succeeded", "approved"}]
    return {
        "total_jobs": len(jobs),
        "generated_count": len(generated),
        "approved_count": sum(1 for job in jobs if job.status == "approved"),
        "running_count": sum(1 for job in jobs if job.status in {"queued", "running"}),
        "failed_count": sum(1 for job in jobs if job.status == "failed"),
        "asset_count": sum(1 for job in generated if job.result and job.result.export_asset_id),
    }


def filter_dashboard_jobs(jobs: list, query: str = "", status: str = "") -> list:
    query = query.strip().lower()
    status = status.strip().lower()
    filtered = jobs
    if status:
        if status == "running":
            filtered = [job for job in filtered if job.status in {"queued", "running"}]
        else:
            filtered = [job for job in filtered if job.status == status]
    if query:
        def matches(job) -> bool:
            fields = [
                job.product_title,
                job.store_slug,
                str(job.nm_id or ""),
                str(job.sku_id or ""),
                job.id,
            ]
            return any(query in field.lower() for field in fields if field)

        filtered = [job for job in filtered if matches(job)]
    return filtered


def dashboard_status_tabs(jobs: list, selected_status: str, query: str) -> list[dict[str, str | int | bool]]:
    counts = {
        "": len(jobs),
        "succeeded": sum(1 for job in jobs if job.status == "succeeded"),
        "approved": sum(1 for job in jobs if job.status == "approved"),
        "running": sum(1 for job in jobs if job.status in {"queued", "running"}),
        "failed": sum(1 for job in jobs if job.status == "failed"),
    }
    tabs = [{"value": "", "label": "Все", "count": counts[""]}]
    tabs.extend(
        {
            "value": status,
            "label": "В работе" if status == "running" else job_status_label(status),
            "count": counts[status],
        }
        for status in PROJECT_STATUS_FILTERS
    )
    clean_query = quote(query.strip())
    for tab in tabs:
        value = str(tab["value"])
        params = []
        if value:
            params.append(f"status={quote(value)}")
        if clean_query:
            params.append(f"q={clean_query}")
        tab["href"] = url("/" + (f"?{'&'.join(params)}" if params else ""))
        tab["active"] = value == selected_status
    return tabs


def wb_product_has_inline_photo(item: dict) -> bool:
    for key in ("images", "photos", "media"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list):
            for image in value:
                if isinstance(image, str) and image.strip():
                    return True
                if isinstance(image, dict) and any(
                    str(image.get(field, "")).strip()
                    for field in ("url", "src", "photo", "image")
                ):
                    return True
    return False


def decorate_wb_product(item: dict, *, account_id: int, account_name: str) -> dict:
    has_photo = wb_product_has_inline_photo(item)
    return {
        **item,
        "account_id": account_id,
        "account_name": account_name,
        "photo_status_label": "Фото есть в WB" if has_photo else "Фото проверим при запуске",
        "photo_status_tone": "ok" if has_photo else "pending",
        "generation_ready": bool(item.get("sku_id")),
    }


async def create_context(request: Request) -> dict:
    wb_error = ""
    accounts: list[dict] = []
    products: list[dict] = []
    selected_account_id = request.query_params.get("account_id", "")
    q = request.query_params.get("q", "")
    brief = request.query_params.get("brief", "")
    selected_template_id = request.query_params.get("category_template_id", "")
    try:
        wb = WBToolClient()
        accounts = await wb.accounts()
        if selected_account_id:
            account = next(
                (
                    item
                    for item in accounts
                    if str(item.get("id", "")) == selected_account_id
                ),
                {},
            )
            result = await wb.products(int(selected_account_id), q=q, limit=100)
            products = [
                decorate_wb_product(
                    item,
                    account_id=int(selected_account_id),
                    account_name=account.get("name") or account.get("slug") or "WB",
                )
                for item in result.get("items", [])
            ]
        elif accounts:
            per_account_limit = 50 if q.strip() else 12
            for account in accounts:
                account_id = account.get("id")
                if account_id is None:
                    continue
                try:
                    result = await wb.products(int(account_id), q=q, limit=per_account_limit)
                except (httpx.HTTPError, ValueError):
                    continue
                account_label = account.get("name") or account.get("slug") or "WB"
                products.extend(
                    decorate_wb_product(
                        item,
                        account_id=int(account_id),
                        account_name=account_label,
                    )
                    for item in result.get("items", [])
                )
    except (httpx.HTTPError, ValueError) as exc:
        wb_error = f"WB Tool недоступен или вернул ошибку: {type(exc).__name__}"
    configured_accounts_count = sum(
        1 for account in accounts if account.get("wb_configured", True)
    )
    return {
        "accounts": accounts,
        "products": products,
        "products_count": len(products),
        "accounts_count": len(accounts),
        "configured_accounts_count": configured_accounts_count,
        "selected_account_id": selected_account_id,
        "is_all_accounts": bool(accounts and not selected_account_id),
        "q": q,
        "brief": brief,
        "category_template_id": selected_template_id,
        "category_templates": list_category_templates(service(request).storage),
        "wb_error": wb_error,
        "wb_tool_base_url": settings.wb_tool_base_url,
    }


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    eff = EffectiveSettings.resolve(service(request).storage)
    return JSONResponse(
        content={
            "ok": True,
            "version": __version__,
            "provider": eff.effective_provider,
            "configured_provider": eff.provider,
            "image_model": eff.openrouter_image_model if eff.effective_provider == "openrouter" else "mock",
            "text_model": eff.openrouter_text_model if eff.effective_provider == "openrouter" else "mock",
            "ai_images": eff.enable_ai_images,
        },
        headers=NO_CACHE_HEADERS,
    )


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    favicon_path = Path(__file__).resolve().parents[1] / "static" / "favicon.svg"
    return FileResponse(favicon_path, media_type="image/svg+xml")


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    jobs = service(request).list_jobs()
    project_query = request.query_params.get("q", "").strip()
    project_status = request.query_params.get("status", "").strip().lower()
    if project_status not in {"", *PROJECT_STATUS_FILTERS}:
        project_status = ""
    filtered_jobs = filter_dashboard_jobs(jobs, project_query, project_status)
    return apply_no_cache(
        request.app.state.templates.TemplateResponse(
            request,
            "index.html",
            {
                "jobs": jobs,
                "project_jobs": filtered_jobs,
                "project_query": project_query,
                "project_status": project_status,
                "project_status_tabs": dashboard_status_tabs(jobs, project_status, project_query),
                "stats": dashboard_stats(jobs),
                "account": {"name": "Локальный кабинет", "support_code": "314046"},
                "wb_tool_base_url": settings.wb_tool_base_url,
                "wb_tool_display_url": settings.wb_tool_display_url,
                "integration_status": "Настроен" if settings.wb_tool_base_url else "Не настроен",
                "base_path": settings.public_base_path,
                "active_page": "dashboard",
                "page_title": "Кабинет",
            },
        )
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    eff = EffectiveSettings.resolve(service(request).storage)
    saved = request.query_params.get("saved") == "ok"
    test_status = request.query_params.get("test", "")
    test_error = request.query_params.get("test_error", "")
    return apply_no_cache(
        request.app.state.templates.TemplateResponse(
            request,
            "settings.html",
            {
                "base_path": settings.public_base_path,
                "active_page": "settings",
                "page_title": "Настройки",
                "provider": eff.provider,
                "effective_provider": eff.effective_provider,
                "openrouter_api_key_masked": mask_api_key(eff.openrouter_api_key),
                "openrouter_text_model": eff.openrouter_text_model,
                "openrouter_image_model": eff.openrouter_image_model,
                "enable_ai_images": eff.enable_ai_images,
                "wb_content_token_masked": mask_api_key(eff.wb_content_api_token),
                "saved": saved,
                "test_status": test_status,
                "test_error": test_error,
            },
        )
    )


@router.post("/settings")
async def save_settings(request: Request) -> RedirectResponse:
    form = await request.form()
    storage = service(request).storage
    current = EffectiveSettings.resolve(storage)
    provider = str(form.get("provider") or "mock").strip().lower()
    if provider not in {"mock", "openrouter"}:
        provider = "mock"
    text_model = str(form.get("openrouter_text_model") or current.openrouter_text_model).strip()
    image_model = str(form.get("openrouter_image_model") or current.openrouter_image_model).strip()
    enable_ai_images = str(form.get("enable_ai_images") or "") == "on"
    api_key_input = str(form.get("openrouter_api_key") or "").strip()
    values: dict[str, str] = {
        "provider": provider,
        "openrouter_text_model": text_model or current.openrouter_text_model,
        "openrouter_image_model": image_model or current.openrouter_image_model,
        "enable_ai_images": "true" if enable_ai_images else "false",
    }
    if api_key_input and not api_key_input.startswith("••••"):
        values["openrouter_api_key"] = api_key_input
    elif current.openrouter_api_key:
        values["openrouter_api_key"] = current.openrouter_api_key
    wb_token_input = str(form.get("wb_content_api_token") or "").strip()
    if wb_token_input and not wb_token_input.startswith("••••"):
        values["wb_content_api_token"] = wb_token_input
    elif current.wb_content_api_token:
        values["wb_content_api_token"] = current.wb_content_api_token
    storage.set_settings(values)
    return RedirectResponse(url=url("/settings?saved=ok"), status_code=303)


@router.post("/settings/test")
async def test_settings(request: Request) -> RedirectResponse:
    eff = EffectiveSettings.resolve(service(request).storage)
    if not eff.openrouter_api_key.strip():
        return RedirectResponse(url=url("/settings?test=missing_key"), status_code=303)
    try:
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            response = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={
                    "Authorization": f"Bearer {eff.openrouter_api_key}",
                    "HTTP-Referer": "http://127.0.0.1:8787",
                    "X-Title": "Identika WB AI",
                },
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return RedirectResponse(
            url=url(f"/settings?test=error&test_error={type(exc).__name__}"),
            status_code=303,
        )
    return RedirectResponse(url=url("/settings?test=ok"), status_code=303)


@router.get("/templates", response_class=HTMLResponse)
async def templates_page(request: Request) -> HTMLResponse:
    return apply_no_cache(
        request.app.state.templates.TemplateResponse(
            request,
            "templates.html",
            {
                "base_path": settings.public_base_path,
                "active_page": "templates",
                "page_title": "Шаблоны",
                "templates": list_category_template_views(service(request).storage),
                "saved": request.query_params.get("saved") == "ok",
                "save_error": request.query_params.get("save_error", ""),
                "deleted": request.query_params.get("deleted") == "ok",
                "delete_error": request.query_params.get("delete_error", ""),
            },
        )
    )


@router.post("/templates")
async def save_template_page(request: Request) -> RedirectResponse:
    form = await request.form()
    data = {str(key): str(value) for key, value in form.items()}
    saved = save_category_template(service(request).storage, template_from_form(data))
    if not saved:
        return RedirectResponse(url=url("/templates?save_error=builtin"), status_code=303)
    return RedirectResponse(url=url("/templates?saved=ok"), status_code=303)


@router.post("/templates/{template_id}/delete")
async def delete_template_page(request: Request, template_id: str) -> RedirectResponse:
    existing = get_category_template(service(request).storage, template_id)
    deleted = delete_category_template(service(request).storage, template_id)
    if not deleted:
        error = "builtin" if existing else "missing"
        return RedirectResponse(url=url(f"/templates?delete_error={error}"), status_code=303)
    return RedirectResponse(url=url("/templates?deleted=ok"), status_code=303)


@router.get("/create", response_class=HTMLResponse)
async def create_page(request: Request) -> HTMLResponse:
    context = await create_context(request)
    context.update(
        {
            "base_path": settings.public_base_path,
            "active_page": "create",
            "page_title": "Создать проект",
            "photo_hint": (
                "Если у товара нет фото в WB, загрузите до 4 фото в блоке «Ваш товар» — "
                "без них генерация не запустится."
            ),
            "photo_error": request.query_params.get("photo_error", ""),
        }
    )
    return apply_no_cache(request.app.state.templates.TemplateResponse(request, "create.html", context))


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: str) -> HTMLResponse:
    try:
        job = service(request).get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    upload_status = request.query_params.get("upload", "")
    upload_detail = request.query_params.get("upload_detail", "")
    upload_status_code = request.query_params.get("upload_status_code", "")
    upload_via = request.query_params.get("upload_via", "")
    photo_status = source_photo_status(job.result.product) if job.result else None
    missing_source_photos = bool(photo_status and photo_status["needs_action"])
    readiness = result_readiness(job.result) if job.result else None
    readiness_blocked = bool(readiness and readiness["blockers"])
    can_edit = bool(job.result and job.status != "approved")
    can_approve = bool(job.result and job.status != "approved" and not readiness_blocked)
    can_export = bool(job.result and job.status == "approved" and job.result.export_asset_id)
    can_rich_export = bool(job.result and job.status == "approved" and job.result.rich.zip_asset_id)
    can_upload = bool(job.result and job.status == "approved" and can_export)
    return apply_no_cache(
        request.app.state.templates.TemplateResponse(
            request,
            "job.html",
            {
                "job": job,
                "base_path": settings.public_base_path,
                "active_page": "jobs",
                "page_title": job.product_title,
                "can_edit": can_edit,
                "can_approve": can_approve,
                "can_export": can_export,
                "can_rich_export": can_rich_export,
                "can_upload": can_upload,
                "upload_status": upload_status,
                "upload_detail": upload_detail,
                "upload_status_code": upload_status_code,
                "upload_via": upload_via,
                "missing_source_photos": missing_source_photos,
                "source_photo_status": photo_status,
                "readiness": readiness,
                "category_templates": list_category_template_views(service(request).storage),
            },
        )
    )


@router.post("/jobs/{job_id}/template")
async def apply_job_template_page(
    request: Request,
    job_id: str,
    category_template_id: str = Form(""),
) -> RedirectResponse:
    try:
        service(request).apply_category_template(job_id, category_template_id.strip() or None)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url=url(f"/jobs/{job_id}?template=applied"), status_code=303)


@router.post("/jobs/{job_id}/re-render")
async def rerender_job_page(request: Request, job_id: str) -> RedirectResponse:
    try:
        await service(request).rerender_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url=url(f"/jobs/{job_id}?rerender=ok"), status_code=303)


@router.post("/v1/generation/jobs/{job_id}/re-render")
async def rerender_job_api(request: Request, job_id: str) -> JSONResponse:
    try:
        job = await service(request).rerender_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(
        content={"ok": True, "job_id": job.id, "status": job.status},
        headers=NO_CACHE_HEADERS,
    )


@router.post("/demo")
async def demo(request: Request, background_tasks: BackgroundTasks):
    payload = CreateJobRequest(
        product={
            "store_slug": "demo",
            "sku_id": 1,
            "nm_id": 1000001,
            "title": "Ночник-проектор звёздного неба",
            "subject_name": "Дом и интерьер",
            "characteristics": {"Питание": "USB", "Режимы": "7 проекций", "Таймер": "1/2/4 часа"},
        },
        brief="Демо-генерация без внешних API.",
        allow_generate_without_photos=True,
    )
    job = await service(request).create_job(payload, background_tasks=background_tasks)
    return RedirectResponse(url=url(f"/jobs/{job.id}"), status_code=303)


@router.post("/wb/generate")
async def generate_from_wb(
    request: Request,
    background_tasks: BackgroundTasks,
    account_id: int = Form(...),
    sku_id: int = Form(...),
    brief: str = Form(""),
    source_image_asset_ids: str = Form(""),
    source_image_urls: str = Form(""),
    allow_generate_without_photos: str = Form(""),
    auto_approve: str = Form(""),
    category_template_id: str = Form(""),
) -> RedirectResponse:
    try:
        internet_urls = parse_source_image_urls(source_image_urls)
    except ValueError as exc:
        return photo_error_redirect(account_id, brief, category_template_id, str(exc))
    try:
        eff = EffectiveSettings.resolve(service(request).storage)
        wb = WBToolClient(wb_content_token=eff.wb_content_api_token)
        product, _image_notes = await wb.resolve_product_images(
            sku_id,
            account_id,
            ProductContext(account_id=account_id, sku_id=sku_id),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"WB Tool error: {type(exc).__name__}") from exc
    uploaded_ids = parse_source_image_ids(source_image_asset_ids)
    attach_source_images(product, uploaded_ids)
    attach_source_image_urls(product, internet_urls)
    allow_without = allow_generate_without_photos in {"1", "true", "on", "yes"}
    should_auto_approve = auto_approve in {"1", "true", "on", "yes"}
    try:
        validate_can_start_generation(product, allow_without_photos=allow_without)
    except SourcePhotosRequiredError as exc:
        return photo_error_redirect(account_id, brief, category_template_id, str(exc))
    try:
        job = await service(request).create_job(
            CreateJobRequest(
                product=product,
                brief=brief,
                style="marketplace-clean",
                outputs=["wb_10_slides", "rich_package"],
                source_image_asset_ids=uploaded_ids,
                allow_generate_without_photos=allow_without,
                auto_approve=should_auto_approve,
                category_template_id=category_template_id.strip() or None,
            ),
            background_tasks=background_tasks,
        )
    except SourcePhotosRequiredError as exc:
        return photo_error_redirect(account_id, brief, category_template_id, str(exc))
    return RedirectResponse(url=url(f"/jobs/{job.id}"), status_code=303)


def _product_from_wb_card(nm_id: int, card: dict | None) -> ProductContext:
    """Build ProductContext from a WB Content API card (or minimal fallback by nmID)."""
    if not card:
        return ProductContext(
            nm_id=nm_id,
            title=f"Товар WB {nm_id}",
        )
    characteristics: dict[str, str] = {}
    for item in card.get("characteristics") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        params = [str(p) for p in (item.get("params") or []) if str(p).strip()]
        if name and params:
            characteristics[name] = ", ".join(params)
    return ProductContext(
        nm_id=nm_id,
        vendor_code=str(card.get("vendorCode")) if card.get("vendorCode") else None,
        title=str(card.get("title") or f"Товар WB {nm_id}")[:200],
        brand=str(card.get("brand")) if card.get("brand") else None,
        subject_name=str(card.get("subjectName")) if card.get("subjectName") else None,
        characteristics=characteristics,
    )


@router.post("/wb/generate-nm")
async def generate_from_nm_id(
    request: Request,
    background_tasks: BackgroundTasks,
    nm_id: int = Form(...),
    brief: str = Form(""),
    source_image_asset_ids: str = Form(""),
    source_image_urls: str = Form(""),
    auto_approve: str = Form(""),
    category_template_id: str = Form(""),
) -> RedirectResponse:
    """Create a job straight from a WB nmID: card data + photos via official Content API,
    CDN fallback when no token is configured. Does not require the external WB Tool."""
    if nm_id <= 0:
        return RedirectResponse(url=url("/create?photo_error=" + quote("Укажите корректный nmID товара WB")), status_code=303)
    try:
        internet_urls = parse_source_image_urls(source_image_urls)
    except ValueError as exc:
        return RedirectResponse(
            url=url(f"/create?photo_error={quote(str(exc))}"),
            status_code=303,
        )
    eff = EffectiveSettings.resolve(service(request).storage)
    try:
        card = await WBContentClient(token=eff.wb_content_api_token).product_card(nm_id)
    except httpx.HTTPError:
        card = None
    product = _product_from_wb_card(nm_id, card)
    photo_urls = photo_urls_from_card(card) if card else []
    if photo_urls:
        product.images = [
            ProductImage(url=photo_url, role="source", alt=f"WB Content API {index}")
            for index, photo_url in enumerate(photo_urls, start=1)
        ]
    uploaded_ids = parse_source_image_ids(source_image_asset_ids)
    attach_source_images(product, uploaded_ids)
    attach_source_image_urls(product, internet_urls)
    # nm_id > 0 keeps generation allowed even without photos (CDN fallback runs inside the job).
    try:
        job = await service(request).create_job(
            CreateJobRequest(
                product=product,
                brief=brief,
                style="marketplace-clean",
                outputs=["wb_10_slides", "rich_package"],
                source_image_asset_ids=uploaded_ids,
                auto_approve=auto_approve in {"1", "true", "on", "yes"},
                category_template_id=category_template_id.strip() or None,
            ),
            background_tasks=background_tasks,
        )
    except SourcePhotosRequiredError as exc:
        return RedirectResponse(
            url=url(f"/create?photo_error={quote(str(exc))}"),
            status_code=303,
        )
    return RedirectResponse(url=url(f"/jobs/{job.id}"), status_code=303)


@router.post("/v1/uploads/source-images")
async def upload_source_images(
    request: Request,
    files: list[UploadFile] = File(...),
    session_id: str | None = Form(None),
) -> dict:
    return await save_source_images(service(request).storage, files, session_id=session_id)


@router.post("/v1/generation/jobs")
async def create_generation_job(
    request: Request,
    payload: CreateJobRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    try:
        job = await service(request).create_job(payload, background_tasks=background_tasks)
    except SourcePhotosRequiredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": job.id,
        "status": job.status,
        "result_url": url(f"/v1/generation/jobs/{job.id}/result"),
        "export_url": url(f"/v1/generation/jobs/{job.id}/export"),
    }


@router.post("/v1/generation/jobs/batch")
async def create_generation_jobs_batch(
    request: Request,
    payload: BatchCreateJobsRequest,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Queue up to 20 jobs in one call (automatic mode for a list of products)."""
    items: list[dict] = []
    errors: list[dict] = []
    for index, job_request in enumerate(payload.jobs):
        try:
            job = await service(request).create_job(job_request, background_tasks=background_tasks)
        except SourcePhotosRequiredError as exc:
            errors.append({"index": index, "detail": str(exc)})
            continue
        items.append(
            {
                "index": index,
                "id": job.id,
                "status": job.status,
                "result_url": url(f"/v1/generation/jobs/{job.id}/result"),
                "export_url": url(f"/v1/generation/jobs/{job.id}/export"),
            }
        )
    return JSONResponse(
        content={"items": items, "errors": errors, "created": len(items)},
        headers=NO_CACHE_HEADERS,
    )


@router.post("/v1/generation/jobs/{job_id}/source-images")
async def attach_job_source_images_api(
    request: Request,
    background_tasks: BackgroundTasks,
    job_id: str,
    source_image_asset_ids: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    source_image_urls: str = Form(""),
) -> JSONResponse:
    asset_ids = parse_source_image_ids(source_image_asset_ids)
    try:
        internet_urls = parse_source_image_urls(source_image_urls)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if files:
        upload = await save_source_images(service(request).storage, files)
        asset_ids.extend(upload["asset_ids"])
    try:
        existing = service(request).get_job(job_id)
        if existing.result:
            job = await service(request).attach_source_images_to_job(job_id, asset_ids, internet_urls)
        else:
            if not asset_ids and not internet_urls:
                raise ValueError("at least one source image is required")
            job = await service(request).retry_failed_job(
                job_id,
                asset_ids,
                internet_urls,
                background_tasks=background_tasks,
            )
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(
        content={
            "ok": True,
            "job_id": job.id,
            "source_assets": sum(
                1 for img in job.result.product.images if img.role == "source" and img.asset_id
            )
            if job.result
            else 0,
        },
        headers=NO_CACHE_HEADERS,
    )


@router.post("/jobs/{job_id}/source-images")
async def attach_job_source_images_page(
    request: Request,
    background_tasks: BackgroundTasks,
    job_id: str,
    source_image_asset_ids: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    source_image_urls: str = Form(""),
) -> RedirectResponse:
    asset_ids = parse_source_image_ids(source_image_asset_ids)
    try:
        internet_urls = parse_source_image_urls(source_image_urls)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if files:
        upload = await save_source_images(service(request).storage, files)
        asset_ids.extend(upload["asset_ids"])
    try:
        existing = service(request).get_job(job_id)
        if existing.result:
            next_job = await service(request).attach_source_images_to_job(job_id, asset_ids, internet_urls)
        else:
            if not asset_ids and not internet_urls:
                raise ValueError("at least one source image is required")
            next_job = await service(request).retry_failed_job(
                job_id,
                asset_ids,
                internet_urls,
                background_tasks=background_tasks,
            )
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url=url(f"/jobs/{next_job.id}?photos=attached"), status_code=303)


@router.post("/jobs/{job_id}/retry")
async def retry_failed_job_page(
    request: Request,
    background_tasks: BackgroundTasks,
    job_id: str,
) -> RedirectResponse:
    try:
        job = await service(request).retry_failed_job(
            job_id,
            allow_without_photos=True,
            background_tasks=background_tasks,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url=url(f"/jobs/{job.id}?retry=ok"), status_code=303)


@router.get("/v1/generation/jobs")
async def list_generation_jobs(request: Request) -> JSONResponse:
    payload = {"items": [job.model_dump(mode="json", exclude={"result"}) for job in service(request).list_jobs()]}
    return JSONResponse(content=payload, headers=NO_CACHE_HEADERS)


@router.get("/v1/generation/jobs/{job_id}")
async def get_generation_job(request: Request, job_id: str) -> JSONResponse:
    try:
        job = service(request).get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    return JSONResponse(content=job.model_dump(mode="json", exclude={"result"}), headers=NO_CACHE_HEADERS)


@router.get("/v1/generation/jobs/{job_id}/result")
async def get_generation_result(request: Request, job_id: str) -> JSONResponse:
    try:
        job = service(request).get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    if not job.result:
        raise HTTPException(status_code=409, detail=f"job is {job.status}")
    return JSONResponse(content=job.result.model_dump(mode="json"), headers=NO_CACHE_HEADERS)


@router.post("/v1/generation/jobs/{job_id}/approve")
async def approve_generation_job(request: Request, job_id: str) -> dict:
    try:
        job = service(request).approve(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.model_dump(mode="json", exclude={"result"})


def _slide_png_urls(job, base_url: str) -> list[str]:
    """Public URLs of finalized slide PNGs (for official WB Content media upload)."""
    if not job.result or not base_url:
        return []
    urls = []
    for slide in job.result.slides:
        if slide.png_asset_id:
            urls.append(f"{base_url}/v1/assets/{slide.png_asset_id}")
    return urls


async def perform_wb_upload(request: Request, job) -> dict:
    """Upload approved media to WB: official Content API first, external WB Tool as fallback."""
    eff = EffectiveSettings.resolve(service(request).storage)
    base_url = public_base_url(request)
    if eff.wb_content_api_token and job.result and job.result.product.nm_id:
        png_urls = _slide_png_urls(job, base_url)
        if png_urls:
            content_result = await WBContentClient(token=eff.wb_content_api_token).upload_media(
                job.result.product.nm_id,
                png_urls,
            )
            if content_result.get("ok"):
                return {**content_result, "via": "wb_content"}
    try:
        tool_result = await WBToolClient(wb_content_token=eff.wb_content_api_token).upload_job(
            job, public_base_url=base_url
        )
    except httpx.HTTPError as exc:
        return {"ok": False, "status": 0, "detail": str(exc)[:240]}
    return {**tool_result, "via": "wb_tool"}


@router.post("/jobs/{job_id}/upload-to-wb")
async def upload_job_to_wb(request: Request, job_id: str) -> RedirectResponse:
    try:
        job = service(request).get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    if job.status != "approved":
        raise HTTPException(status_code=409, detail="upload to WB is allowed only after approve")
    result = await perform_wb_upload(request, job)
    query = upload_redirect_query(result)
    if result.get("via") == "wb_content":
        query += "&upload_via=wb_content"
    return RedirectResponse(url=url(f"/jobs/{job_id}?{query}"), status_code=303)


@router.post("/jobs/{job_id}/delete")
async def delete_job_form(request: Request, job_id: str) -> RedirectResponse:
    try:
        service(request).delete_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    return RedirectResponse(url=url("/"), status_code=303)


@router.delete("/v1/generation/jobs/{job_id}")
async def delete_generation_job(request: Request, job_id: str) -> JSONResponse:
    try:
        service(request).delete_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    return JSONResponse(content={"ok": True, "job_id": job_id}, headers=NO_CACHE_HEADERS)


@router.patch("/v1/generation/jobs/{job_id}/result/text")
async def patch_generation_result_text(
    request: Request,
    job_id: str,
    payload: ResultTextPatch,
) -> dict:
    try:
        job = service(request).patch_result_text(job_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not job.result:
        raise HTTPException(status_code=409, detail=f"job is {job.status}")
    return job.result.model_dump(mode="json")


@router.post("/jobs/{job_id}/slides/{slide_index}/text")
async def update_slide_text_form(
    request: Request,
    job_id: str,
    slide_index: int,
) -> RedirectResponse:
    form = await request.form()
    bullets_raw = str(form.get("bullets") or "")
    update = SlideTextUpdate(
        title=str(form.get("title") or ""),
        subtitle=str(form.get("subtitle") or ""),
        bullets=[line.strip() for line in bullets_raw.splitlines() if line.strip()],
    )
    try:
        service(request).update_slide_text(job_id, slide_index, update)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url=url(f"/jobs/{job_id}"), status_code=303)


@router.post("/jobs/{job_id}/slides/{slide_index}/text/reset")
async def reset_slide_text_form(request: Request, job_id: str, slide_index: int) -> RedirectResponse:
    try:
        service(request).reset_slide_text(job_id, slide_index)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url=url(f"/jobs/{job_id}"), status_code=303)


@router.post("/jobs/{job_id}/slides/{slide_index}/image/clear")
async def clear_slide_image_form(request: Request, job_id: str, slide_index: int) -> RedirectResponse:
    try:
        service(request).clear_slide_image(job_id, slide_index)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url=url(f"/jobs/{job_id}"), status_code=303)


@router.delete("/v1/generation/jobs/{job_id}/slides/{slide_index}/text")
async def reset_slide_text_api(request: Request, job_id: str, slide_index: int) -> JSONResponse:
    try:
        service(request).reset_slide_text(job_id, slide_index)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(content={"ok": True, "job_id": job_id, "slide_index": slide_index}, headers=NO_CACHE_HEADERS)


@router.delete("/v1/generation/jobs/{job_id}/slides/{slide_index}/image")
async def clear_slide_image_api(request: Request, job_id: str, slide_index: int) -> JSONResponse:
    try:
        service(request).clear_slide_image(job_id, slide_index)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(content={"ok": True, "job_id": job_id, "slide_index": slide_index}, headers=NO_CACHE_HEADERS)


@router.get("/v1/generation/jobs/{job_id}/export")
async def export_generation_job(request: Request, job_id: str) -> FileResponse:
    try:
        job = service(request).get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    if job.status != "approved":
        raise HTTPException(status_code=409, detail="export is allowed only after approve")
    if not job.result or not job.result.export_asset_id:
        raise HTTPException(status_code=409, detail=f"job is {job.status}")
    path, media_type = service(request).storage.get_asset(job.result.export_asset_id)
    return FileResponse(path, media_type=media_type, filename=f"identika_{job_id}.zip")


@router.get("/v1/generation/jobs/{job_id}/rich-export")
async def rich_export_generation_job(request: Request, job_id: str) -> FileResponse:
    try:
        job = service(request).get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    if job.status != "approved":
        raise HTTPException(status_code=409, detail="rich export is allowed only after approve")
    if not job.result or not job.result.rich.zip_asset_id:
        raise HTTPException(status_code=409, detail=f"job is {job.status}")
    path, media_type = service(request).storage.get_asset(job.result.rich.zip_asset_id)
    return FileResponse(path, media_type=media_type, filename=f"identika_rich_{job_id}.zip")


@router.get("/v1/assets/{asset_id}")
async def get_asset(request: Request, asset_id: str) -> FileResponse:
    try:
        path, media_type = service(request).storage.get_asset(asset_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="asset not found") from None
    return FileResponse(path, media_type=media_type)
