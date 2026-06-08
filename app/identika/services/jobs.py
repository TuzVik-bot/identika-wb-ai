from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from identika.config import EffectiveSettings, settings
from identika.models import (
    CreateJobRequest,
    GenerationResult,
    JobRecord,
    QualityMode,
    RichBlock,
    ResultTextPatch,
    SlideTextUpdate,
    TextBlock,
)
from identika.providers.openrouter import get_provider
from identika.services.category_templates import find_template_for_product, get_category_template
from identika.services.product_images import (
    attach_source_images,
    download_product_images,
    ensure_source_assets_after_download,
    has_source_assets,
    prepare_job_request,
    validate_can_start_generation,
)
from identika.services.rendering import (
    build_rich_zip,
    build_export_zip,
    image_to_data_uri,
    render_pdf_preview,
    render_rich_block_image,
    render_rich_html_preview,
    render_slide_image,
    render_slide_svg,
)
from identika.storage import Storage

if TYPE_CHECKING:
    from fastapi import BackgroundTasks

logger = logging.getLogger("identika.jobs")


class JobService:
    def __init__(self, storage: Storage | None = None) -> None:
        self.storage = storage or Storage()

    async def create_job(
        self,
        request: CreateJobRequest,
        background_tasks: BackgroundTasks | None = None,
    ) -> JobRecord:
        request = prepare_job_request(request)
        validate_can_start_generation(
            request.product,
            allow_without_photos=request.allow_generate_without_photos,
        )
        job = self.storage.create_job(request.model_dump(mode="json"))
        eff = EffectiveSettings.resolve(self.storage)
        if (
            background_tasks is not None
            and eff.effective_provider == "openrouter"
            and eff.enable_ai_images
        ):
            background_tasks.add_task(self._run_job_task, job.id, request)
            return self.storage.get_job(job.id)
        await self._run_job(job.id, request)
        return self.storage.get_job(job.id)

    def _run_job_task(self, job_id: str, request: CreateJobRequest) -> None:
        import asyncio

        asyncio.run(self._run_job(job_id, request))

    async def _run_job(self, job_id: str, request: CreateJobRequest) -> None:
        started = time.perf_counter()
        self.storage.set_running(job_id)
        eff = EffectiveSettings.resolve(self.storage)
        provider_name = eff.effective_provider
        try:
            request.product, image_warnings = await download_product_images(
                job_id, request.product, self.storage
            )
            ensure_source_assets_after_download(
                request.product,
                allow_without_photos=request.allow_generate_without_photos,
            )
            provider = get_provider(self.storage)
            result = await provider.generate(request, eff)
            result.product = request.product
            result.category_template_id = request.category_template_id
            if image_warnings:
                result.warnings.extend(image_warnings)
            if eff.enable_ai_images and eff.effective_provider == "openrouter":
                from identika.providers.image_gen import generate_slide_images

                result = await generate_slide_images(job_id, request, result, self.storage, eff)
            result = self._render_assets(job_id, result)
            self.storage.save_result(job_id, result)
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "job completed",
                extra={"job_id": job_id, "provider": provider_name, "duration_ms": duration_ms},
            )
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "job failed",
                extra={"job_id": job_id, "provider": provider_name, "duration_ms": duration_ms},
            )
            self.storage.save_error(job_id, str(exc))
            raise

    def _source_image_hrefs(self, result: GenerationResult, *, embed: bool) -> list[str]:
        hrefs: list[str] = []
        for image in result.product.images:
            if image.role != "source" or not image.asset_id:
                continue
            href = self._asset_image_href(image.asset_id, embed=embed)
            if href:
                hrefs.append(href)
        return hrefs

    def _asset_image_href(self, asset_id: str | None, *, embed: bool) -> str | None:
        if not asset_id:
            return None
        try:
            path, media_type = self.storage.get_asset(asset_id)
            if not media_type.startswith("image/") or media_type.endswith("svg+xml"):
                return None
            if embed:
                return image_to_data_uri(path, media_type)
            base = settings.public_base_path
            return f"{base}/v1/assets/{asset_id}"
        except (KeyError, ValueError):
            return None

    def _source_href_for_slide(self, slide, source_hrefs: list[str]) -> str | None:
        """Pick product photo per slide: primary for hero/description, gallery rotation for white BG."""
        if not source_hrefs:
            return None
        if slide.role in ("hero", "description"):
            return source_hrefs[0]
        if slide.role == "white_background":
            gallery_idx = max(0, slide.index - 6)
            return source_hrefs[gallery_idx % len(source_hrefs)]
        return source_hrefs[0]

    def _render_slide_hrefs(
        self,
        slide,
        source_hrefs: list[str],
        *,
        embed: bool,
    ) -> tuple[str | None, str | None]:
        if slide.image_cleared:
            return None, None
        source_href = self._source_href_for_slide(slide, source_hrefs)
        background_href = self._asset_image_href(slide.background_asset_id, embed=embed)
        if slide.role == "white_background":
            if source_href:
                return source_href, None
            return None, background_href
        if slide.role == "description" and source_href:
            return source_href, None
        return source_href, background_href

    def _set_slide_text_blocks(self, slide) -> None:
        slide.text_blocks = [block for block in slide.text_blocks if block.kind not in {"title", "subtitle"}]
        slide.text_blocks.insert(0, TextBlock(kind="subtitle", text=slide.subtitle, y=0.24, size=28))
        slide.text_blocks.insert(0, TextBlock(kind="title", text=slide.title))

    def _default_slide_text(self, slide) -> tuple[str, str]:
        if slide.index == 10:
            return "Комплект поставки", "Что входит в набор"
        if slide.role == "hero":
            return "Ключевое преимущество", "Краткий оффер для первого экрана"
        if slide.role == "description":
            return "Преимущество товара", "Краткое описание пользы"
        return f"Ракурс {slide.index}", "Фото для галереи"

    def _validate_rich_content(self, result: GenerationResult) -> GenerationResult:
        valid_blocks = [b for b in result.rich.blocks if b.title.strip() or b.text.strip()]
        if len(valid_blocks) < 3:
            rebuilt: list[RichBlock] = []
            for slide in result.slides[:5]:
                rebuilt.append(
                    RichBlock(
                        index=slide.index,
                        title=slide.title or f"Слайд {slide.index}",
                        text=slide.subtitle or "Контент подготовлен автоматически.",
                    )
                )
            result.rich.blocks = rebuilt
            result.warnings.append(
                "Rich-контент частично деградировал: собран fallback-пакет из слайдов, проверьте блоки перед экспортом."
            )
        return result

    def _render_assets(
        self,
        job_id: str,
        result: GenerationResult,
        *,
        quality_mode: QualityMode = "preview",
    ) -> GenerationResult:
        result = self._validate_rich_content(result)
        result.quality_mode = quality_mode
        category_template = get_category_template(
            self.storage,
            result.category_template_id,
        ) or find_template_for_product(self.storage, result.product)
        if category_template:
            result.category_template_id = category_template.id
        source_hrefs_export = self._source_image_hrefs(result, embed=True)
        asset_blobs: dict[str, bytes] = {}
        export_blobs: dict[str, bytes] = {}
        for slide in result.slides:
            export_source, export_background = self._render_slide_hrefs(
                slide, source_hrefs_export, embed=True
            )
            data = render_slide_svg(
                slide,
                source_image_href=export_source,
                background_image_href=export_background,
                category_template=category_template,
            )
            asset_id = self.storage.add_asset(job_id, f"slide_{slide.index:02d}.svg", data, "image/svg+xml")
            slide.asset_id = asset_id
            asset_blobs[asset_id] = data
            export_blobs[asset_id] = render_slide_image(
                slide,
                source_image_href=export_source,
                background_image_href=export_background,
                category_template=category_template,
            )
        if result.slides:
            export_source, export_background = self._render_slide_hrefs(
                result.slides[0], source_hrefs_export, embed=True
            )
            cover_data = render_slide_svg(
                result.slides[0],
                source_image_href=export_source,
                background_image_href=export_background,
                category_template=category_template,
            )
            result.rich.cover_asset_id = self.storage.add_asset(
                job_id, "rich_cover.svg", cover_data, "image/svg+xml"
            )
            asset_blobs[result.rich.cover_asset_id] = cover_data
            export_blobs[result.rich.cover_asset_id] = render_slide_svg(
                result.slides[0],
                source_image_href=export_source,
                background_image_href=export_background,
                category_template=category_template,
            )
        for block in result.rich.blocks:
            source = result.slides[min(block.index - 1, len(result.slides) - 1)]
            export_source, _export_background = self._render_slide_hrefs(
                source, source_hrefs_export, embed=True
            )
            data = render_rich_block_image(
                block,
                result.product,
                source_image_href=export_source,
            )
            block.asset_id = self.storage.add_asset(
                job_id, f"rich_block_{block.index:02d}.png", data, "image/png"
            )
            asset_blobs[block.asset_id] = data
            export_blobs[block.asset_id] = data
        pdf = render_pdf_preview(result)
        result.rich.pdf_asset_id = self.storage.add_asset(job_id, "rich_preview.pdf", pdf, "application/pdf")
        asset_blobs[result.rich.pdf_asset_id] = pdf
        export_blobs[result.rich.pdf_asset_id] = pdf
        rich_html = render_rich_html_preview(result)
        result.rich.html_asset_id = self.storage.add_asset(
            job_id, "rich_preview.html", rich_html, "text/html; charset=utf-8"
        )
        asset_blobs[result.rich.html_asset_id] = rich_html
        export_blobs[result.rich.html_asset_id] = rich_html
        rich_zip = build_rich_zip(result, export_blobs)
        result.rich.zip_asset_id = self.storage.add_asset(job_id, "rich_export.zip", rich_zip, "application/zip")
        asset_blobs[result.rich.zip_asset_id] = rich_zip
        export_blobs[result.rich.zip_asset_id] = rich_zip
        export = build_export_zip(result, export_blobs, quality_mode=quality_mode)
        result.export_asset_id = self.storage.add_asset(job_id, "export.zip", export, "application/zip")
        result.info = [
            item
            for item in result.info
            if "Режим качества:" not in item
        ]
        result.info.append(
            "Режим качества: "
            + ("preview (до approve)" if quality_mode == "preview" else "finalize WB export (после approve)")
        )
        result.info = [
            item
            for item in result.info
            if "Шаблон категории:" not in item
        ]
        if category_template:
            result.info.append(f"Шаблон категории: {category_template.name}")
        return result

    def update_slide_text(self, job_id: str, slide_index: int, update: SlideTextUpdate) -> JobRecord:
        job = self.storage.get_job(job_id)
        if not job.result:
            raise ValueError("job has no result")
        if job.status == "approved":
            raise ValueError("approved job cannot be edited")
        slide = next((item for item in job.result.slides if item.index == slide_index), None)
        if slide is None:
            raise ValueError("slide not found")
        if update.title is not None:
            slide.title = update.title.strip()
        if update.subtitle is not None:
            slide.subtitle = update.subtitle.strip()
        if update.bullets is not None:
            slide.bullets = [item.strip() for item in update.bullets if item.strip()]
        self._set_slide_text_blocks(slide)
        job.result = self._render_assets(job_id, job.result, quality_mode="preview")
        self.storage.update_result(job_id, job.result)
        return self.storage.get_job(job_id)

    def patch_result_text(self, job_id: str, patch: ResultTextPatch) -> JobRecord:
        job = self.storage.get_job(job_id)
        if not job.result:
            raise ValueError("job has no result")
        if job.status == "approved":
            raise ValueError("approved job cannot be edited")

        for slide_patch in patch.slides:
            slide = next((item for item in job.result.slides if item.index == slide_patch.index), None)
            if slide is None:
                raise ValueError(f"slide {slide_patch.index} not found")
            if slide_patch.title is not None:
                slide.title = slide_patch.title.strip()
            if slide_patch.subtitle is not None:
                slide.subtitle = slide_patch.subtitle.strip()
            if slide_patch.bullets is not None:
                slide.bullets = [item.strip() for item in slide_patch.bullets if item.strip()]
            self._set_slide_text_blocks(slide)

        for block_patch in patch.rich_blocks:
            block = next((item for item in job.result.rich.blocks if item.index == block_patch.index), None)
            if block is None:
                raise ValueError(f"rich block {block_patch.index} not found")
            if block_patch.title is not None:
                block.title = block_patch.title.strip()
            if block_patch.text is not None:
                block.text = block_patch.text.strip()

        job.result = self._render_assets(job_id, job.result, quality_mode="preview")
        self.storage.update_result(job_id, job.result)
        return self.storage.get_job(job_id)

    def list_jobs(self) -> list[JobRecord]:
        return self.storage.list_jobs()

    def get_job(self, job_id: str) -> JobRecord:
        return self.storage.get_job(job_id)

    def approve(self, job_id: str) -> JobRecord:
        job = self.storage.get_job(job_id)
        if not job.result:
            raise ValueError("job has no result")
        if job.status not in ("succeeded", "approved"):
            raise ValueError("approve is allowed only after successful generation")
        if job.result.quality_mode != "final":
            job.result = self._render_assets(job_id, job.result, quality_mode="final")
            self.storage.update_result(job_id, job.result)
        return self.storage.approve(job_id)

    def apply_category_template(self, job_id: str, template_id: str | None) -> JobRecord:
        job = self.storage.get_job(job_id)
        if not job.result:
            raise ValueError("job has no result")
        clean = (template_id or "").strip()
        if clean:
            template = get_category_template(self.storage, clean)
            if template is None:
                raise ValueError("category template not found")
            job.result.category_template_id = template.id
        else:
            job.result.category_template_id = None
        quality_mode: QualityMode = "final" if job.status == "approved" else "preview"
        job.result = self._render_assets(job_id, job.result, quality_mode=quality_mode)
        self.storage.update_result(job_id, job.result)
        return self.storage.get_job(job_id)

    async def rerender_job(self, job_id: str) -> JobRecord:
        """Re-download missing product photos and re-render slide SVGs (fixes broken embeds)."""
        job = self.storage.get_job(job_id)
        if not job.result:
            raise ValueError("job has no result")
        product = job.result.product
        if not has_source_assets(product):
            product, image_warnings = await download_product_images(job_id, product, self.storage)
            job.result.product = product
            if image_warnings:
                kept = [
                    w
                    for w in job.result.warnings
                    if "фото" not in w.lower() and "cdn" not in w.lower()
                ]
                job.result.warnings = kept + image_warnings
        quality_mode: QualityMode = "final" if job.status == "approved" else "preview"
        job.result = self._render_assets(job_id, job.result, quality_mode=quality_mode)
        self.storage.update_result(job_id, job.result)
        return self.storage.get_job(job_id)

    async def attach_source_images_to_job(
        self,
        job_id: str,
        asset_ids: list[str],
    ) -> JobRecord:
        job = self.storage.get_job(job_id)
        if not job.result:
            raise ValueError("job has no result")
        if job.status == "approved":
            raise ValueError("approved job cannot be edited")
        if not asset_ids:
            raise ValueError("at least one source image is required")
        attach_source_images(job.result.product, asset_ids)
        quality_mode: QualityMode = "final" if job.status == "approved" else "preview"
        job.result = self._render_assets(job_id, job.result, quality_mode=quality_mode)
        self.storage.update_result(job_id, job.result)
        return self.storage.get_job(job_id)

    def reset_slide_text(self, job_id: str, slide_index: int) -> JobRecord:
        job = self.storage.get_job(job_id)
        if not job.result:
            raise ValueError("job has no result")
        if job.status == "approved":
            raise ValueError("approved job cannot be edited")
        slide = next((item for item in job.result.slides if item.index == slide_index), None)
        if slide is None:
            raise ValueError("slide not found")
        title, subtitle = self._default_slide_text(slide)
        slide.title = title
        slide.subtitle = subtitle
        slide.bullets = []
        self._set_slide_text_blocks(slide)
        job.result = self._render_assets(job_id, job.result, quality_mode="preview")
        self.storage.update_result(job_id, job.result)
        return self.storage.get_job(job_id)

    def clear_slide_image(self, job_id: str, slide_index: int) -> JobRecord:
        job = self.storage.get_job(job_id)
        if not job.result:
            raise ValueError("job has no result")
        if job.status == "approved":
            raise ValueError("approved job cannot be edited")
        slide = next((item for item in job.result.slides if item.index == slide_index), None)
        if slide is None:
            raise ValueError("slide not found")
        slide.image_cleared = True
        slide.background_asset_id = None
        job.result = self._render_assets(job_id, job.result, quality_mode="preview")
        self.storage.update_result(job_id, job.result)
        return self.storage.get_job(job_id)

    def delete_job(self, job_id: str) -> None:
        self.storage.delete_job(job_id)
