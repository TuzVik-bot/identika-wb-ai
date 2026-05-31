from __future__ import annotations

import httpx
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from identika import __version__
from identika.config import settings
from identika.models import CreateJobRequest, ProductContext, ResultTextPatch, SlideTextUpdate
from identika.services.jobs import JobService
from identika.services.product_images import attach_source_images
from identika.services.uploads import save_source_images
from identika.services.wb_tool import WBToolClient

router = APIRouter()


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
async def health() -> dict:
    return {
        "ok": True,
        "version": __version__,
        "provider": settings.effective_provider,
        "configured_provider": settings.provider,
        "image_model": settings.openrouter_image_model
        if settings.effective_provider == "openrouter"
        else "mock",
        "text_model": settings.openrouter_text_model
        if settings.effective_provider == "openrouter"
        else "mock",
        "ai_images": settings.enable_ai_images,
    }


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
            "integration_status": "Настроен" if settings.wb_tool_base_url else "Не настроен",
            "base_path": settings.public_base_path,
            "active_page": "dashboard",
            "page_title": "Кабинет",
        },
    )


@router.get("/create", response_class=HTMLResponse)
async def create_page(request: Request) -> HTMLResponse:
    context = await create_context(request)
    context.update(
        {
            "base_path": settings.public_base_path,
            "active_page": "create",
            "page_title": "Создать проект",
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
    can_upload = bool(job.result and job.status == "approved")
    upload_status = request.query_params.get("upload", "")
    return request.app.state.templates.TemplateResponse(
        request,
        "job.html",
        {
            "job": job,
            "base_path": settings.public_base_path,
            "active_page": "jobs",
            "page_title": job.product_title,
            "can_edit": can_edit,
            "can_approve": can_approve,
            "can_upload": can_upload,
            "upload_status": upload_status,
        },
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
) -> RedirectResponse:
    try:
        context = await WBToolClient().product_context(sku_id, account_id)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"WB Tool error: {type(exc).__name__}") from exc
    product = ProductContext.model_validate(context)
    attach_source_images(product, parse_source_image_ids(source_image_asset_ids))
    job = await service(request).create_job(
        CreateJobRequest(
            product=product,
            brief=brief,
            style="marketplace-clean",
            outputs=["wb_10_slides", "rich_package"],
            source_image_asset_ids=parse_source_image_ids(source_image_asset_ids),
        ),
        background_tasks=background_tasks,
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
    job = await service(request).create_job(payload, background_tasks=background_tasks)
    return {
        "id": job.id,
        "status": job.status,
        "result_url": url(f"/v1/generation/jobs/{job.id}/result"),
        "export_url": url(f"/v1/generation/jobs/{job.id}/export"),
    }


@router.get("/v1/generation/jobs")
async def list_generation_jobs(request: Request) -> dict:
    return {"items": [job.model_dump(mode="json", exclude={"result"}) for job in service(request).list_jobs()]}


@router.get("/v1/generation/jobs/{job_id}")
async def get_generation_job(request: Request, job_id: str) -> dict:
    try:
        job = service(request).get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    return job.model_dump(mode="json", exclude={"result"})


@router.get("/v1/generation/jobs/{job_id}/result")
async def get_generation_result(request: Request, job_id: str) -> dict:
    try:
        job = service(request).get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    if not job.result:
        raise HTTPException(status_code=409, detail=f"job is {job.status}")
    return job.result.model_dump(mode="json")


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
        return RedirectResponse(url=url(f"/jobs/{job_id}?upload=error"), status_code=303)
    if not result.get("ok"):
        return RedirectResponse(url=url(f"/jobs/{job_id}?upload=error"), status_code=303)
    return RedirectResponse(url=url(f"/jobs/{job_id}?upload=ok"), status_code=303)


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


@router.get("/v1/assets/{asset_id}")
async def get_asset(request: Request, asset_id: str) -> FileResponse:
    try:
        path, media_type = service(request).storage.get_asset(asset_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="asset not found") from None
    return FileResponse(path, media_type=media_type)
