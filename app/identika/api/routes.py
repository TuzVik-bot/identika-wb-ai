from __future__ import annotations

from urllib.parse import quote

import httpx
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from identika import __version__
from identika.config import EffectiveSettings, mask_api_key, settings
from identika.models import CreateJobRequest, ProductContext, ResultTextPatch, SlideTextUpdate
from identika.services.jobs import JobService
from identika.services.product_images import (
    SourcePhotosRequiredError,
    attach_source_images,
    has_source_assets,
    validate_can_start_generation,
)
from identika.services.uploads import save_source_images
from identika.services.wb_tool import WBToolClient, upload_redirect_query

router = APIRouter()

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}


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


async def create_context(request: Request) -> dict:
    wb_error = ""
    accounts: list[dict] = []
    products: list[dict] = []
    selected_account_id = request.query_params.get("account_id", "")
    q = request.query_params.get("q", "")
    brief = request.query_params.get("brief", "")
    try:
        wb = WBToolClient()
        accounts = await wb.accounts()
        if selected_account_id:
            result = await wb.products(int(selected_account_id), q=q, limit=100)
            products = result.get("items", [])
    except (httpx.HTTPError, ValueError) as exc:
        wb_error = f"WB Tool недоступен или вернул ошибку: {type(exc).__name__}"
    return {
        "accounts": accounts,
        "products": products,
        "selected_account_id": selected_account_id,
        "q": q,
        "brief": brief,
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


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    jobs = service(request).list_jobs()
    return request.app.state.templates.TemplateResponse(
        request,
        "index.html",
        {
            "jobs": jobs,
            "recent_jobs": jobs[:5],
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
    storage.set_settings(values)
    return RedirectResponse(url=url("/settings?saved=ok"), status_code=303)


@router.post("/settings/test")
async def test_settings(request: Request) -> RedirectResponse:
    eff = EffectiveSettings.resolve(service(request).storage)
    if not eff.openrouter_api_key.strip():
        return RedirectResponse(url=url("/settings?test=missing_key"), status_code=303)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
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
    return request.app.state.templates.TemplateResponse(request, "create.html", context)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: str) -> HTMLResponse:
    try:
        job = service(request).get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    can_edit = bool(job.result and job.status != "approved")
    can_approve = bool(job.result and job.status != "approved")
    can_export = bool(job.result and job.result.export_asset_id)
    can_rich_export = bool(job.result and job.result.rich.zip_asset_id)
    can_upload = bool(job.result and job.status == "approved" and can_export)
    upload_status = request.query_params.get("upload", "")
    upload_detail = request.query_params.get("upload_detail", "")
    missing_source_photos = bool(job.result and not has_source_assets(job.result.product))
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
                "missing_source_photos": missing_source_photos,
            },
        )
    )


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
    allow_generate_without_photos: str = Form(""),
) -> RedirectResponse:
    try:
        wb = WBToolClient()
        product, _image_notes = await wb.resolve_product_images(
            sku_id,
            account_id,
            ProductContext(account_id=account_id, sku_id=sku_id),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"WB Tool error: {type(exc).__name__}") from exc
    uploaded_ids = parse_source_image_ids(source_image_asset_ids)
    attach_source_images(product, uploaded_ids)
    allow_without = allow_generate_without_photos in {"1", "true", "on", "yes"}
    try:
        validate_can_start_generation(product, allow_without_photos=allow_without)
    except SourcePhotosRequiredError as exc:
        return RedirectResponse(
            url=url(f"/create?account_id={account_id}&brief={quote(brief)}&photo_error={quote(str(exc))}"),
            status_code=303,
        )
    try:
        job = await service(request).create_job(
            CreateJobRequest(
                product=product,
                brief=brief,
                style="marketplace-clean",
                outputs=["wb_10_slides", "rich_package"],
                source_image_asset_ids=uploaded_ids,
                allow_generate_without_photos=allow_without,
            ),
            background_tasks=background_tasks,
        )
    except SourcePhotosRequiredError as exc:
        return RedirectResponse(
            url=url(f"/create?account_id={account_id}&brief={quote(brief)}&photo_error={quote(str(exc))}"),
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


@router.post("/v1/generation/jobs/{job_id}/source-images")
async def attach_job_source_images_api(
    request: Request,
    job_id: str,
    source_image_asset_ids: str = Form(""),
    files: list[UploadFile] = File(default=[]),
) -> JSONResponse:
    asset_ids = parse_source_image_ids(source_image_asset_ids)
    if files:
        upload = await save_source_images(service(request).storage, files)
        asset_ids.extend(upload["asset_ids"])
    try:
        job = await service(request).attach_source_images_to_job(job_id, asset_ids)
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
    job_id: str,
    source_image_asset_ids: str = Form(""),
    files: list[UploadFile] = File(default=[]),
) -> RedirectResponse:
    asset_ids = parse_source_image_ids(source_image_asset_ids)
    if files:
        upload = await save_source_images(service(request).storage, files)
        asset_ids.extend(upload["asset_ids"])
    try:
        await service(request).attach_source_images_to_job(job_id, asset_ids)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url=url(f"/jobs/{job_id}?photos=attached"), status_code=303)


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


@router.post("/jobs/{job_id}/upload-to-wb")
async def upload_job_to_wb(request: Request, job_id: str) -> RedirectResponse:
    try:
        job = service(request).get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    if job.status != "approved":
        raise HTTPException(status_code=409, detail="upload to WB is allowed only after approve")
    try:
        result = await WBToolClient().upload_job(job, public_base_url(request))
    except httpx.HTTPError as exc:
        detail = quote(str(exc)[:240])
        return RedirectResponse(
            url=url(f"/jobs/{job_id}?upload=error&upload_detail={detail}"),
            status_code=303,
        )
    query = upload_redirect_query(result)
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
