from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from identika.config import EffectiveSettings, settings
from identika.models import (
    CreateJobRequest,
    GenerationResult,
    JobRecord,
    ResultTextPatch,
    SlideTextUpdate,
    TextBlock,
)
from identika.providers.openrouter import get_provider
from identika.services.product_images import download_product_images, prepare_job_request
from identika.services.rendering import (
    build_export_zip,
    image_to_data_uri,
    render_pdf_preview,
    render_rich_html_preview,
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
            provider = get_provider(self.storage)
            result = await provider.generate(request, eff)
            result.product = request.product
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

    def _render_slide_hrefs(
        self,
        slide,
        source_hrefs: list[str],
        *,
        embed: bool,
    ) -> tuple[str | None, str | None]:
        source_href = source_hrefs[(slide.index - 1) % len(source_hrefs)] if source_hrefs else None
        background_href = self._asset_image_href(slide.background_asset_id, embed=embed)
        if slide.role == "white_background":
            if source_href:
                return source_href, None
            return None, background_href
        if slide.role == "description" and source_href:
            return source_href, None
        return source_href, background_href

    def _render_assets(self, job_id: str, result: GenerationResult) -> GenerationResult:
        source_hrefs_web = self._source_image_hrefs(result, embed=False)
        source_hrefs_export = self._source_image_hrefs(result, embed=True)
        asset_blobs: dict[str, bytes] = {}
        export_blobs: dict[str, bytes] = {}
        for slide in result.slides:
            source_href, background_href = self._render_slide_hrefs(
                slide, source_hrefs_web, embed=False
            )
            data = render_slide_svg(
                slide,
                source_image_href=source_href,
                background_image_href=background_href,
            )
            asset_id = self.storage.add_asset(job_id, f"slide_{slide.index:02d}.svg", data, "image/svg+xml")
            slide.asset_id = asset_id
            asset_blobs[asset_id] = data
            export_source, export_background = self._render_slide_hrefs(
                slide, source_hrefs_export, embed=True
            )
            export_blobs[asset_id] = render_slide_svg(
                slide,
                source_image_href=export_source,
                background_image_href=export_background,
            )
        if result.slides:
            source_href, background_href = self._render_slide_hrefs(
                result.slides[0], source_hrefs_web, embed=False
            )
            cover_data = render_slide_svg(
                result.slides[0],
                source_image_href=source_href,
                background_image_href=background_href,
            )
            result.rich.cover_asset_id = self.storage.add_asset(
                job_id, "rich_cover.svg", cover_data, "image/svg+xml"
            )
            asset_blobs[result.rich.cover_asset_id] = cover_data
            export_source, export_background = self._render_slide_hrefs(
                result.slides[0], source_hrefs_export, embed=True
            )
            export_blobs[result.rich.cover_asset_id] = render_slide_svg(
                result.slides[0],
                source_image_href=export_source,
                background_image_href=export_background,
            )
        for block in result.rich.blocks:
            source = result.slides[min(block.index - 1, len(result.slides) - 1)]
            source_href, background_href = self._render_slide_hrefs(source, source_hrefs_web, embed=False)
            data = render_slide_svg(
                source,
                source_image_href=source_href,
                background_image_href=background_href,
            )
            block.asset_id = self.storage.add_asset(
                job_id, f"rich_block_{block.index:02d}.svg", data, "image/svg+xml"
            )
            asset_blobs[block.asset_id] = data
            export_source, export_background = self._render_slide_hrefs(
                source, source_hrefs_export, embed=True
            )
            export_blobs[block.asset_id] = render_slide_svg(
                source,
                source_image_href=export_source,
                background_image_href=export_background,
            )
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
        export = build_export_zip(result, export_blobs)
        result.export_asset_id = self.storage.add_asset(job_id, "export.zip", export, "application/zip")
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
        slide.text_blocks = [block for block in slide.text_blocks if block.kind not in {"title", "subtitle"}]
        slide.text_blocks.insert(0, TextBlock(kind="subtitle", text=slide.subtitle, y=0.24, size=28))
        slide.text_blocks.insert(0, TextBlock(kind="title", text=slide.title))
        job.result = self._render_assets(job_id, job.result)
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
            slide.text_blocks = [
                block for block in slide.text_blocks if block.kind not in {"title", "subtitle"}
            ]
            slide.text_blocks.insert(0, TextBlock(kind="subtitle", text=slide.subtitle, y=0.24, size=28))
            slide.text_blocks.insert(0, TextBlock(kind="title", text=slide.title))

        for block_patch in patch.rich_blocks:
            block = next((item for item in job.result.rich.blocks if item.index == block_patch.index), None)
            if block is None:
                raise ValueError(f"rich block {block_patch.index} not found")
            if block_patch.title is not None:
                block.title = block_patch.title.strip()
            if block_patch.text is not None:
                block.text = block_patch.text.strip()

        job.result = self._render_assets(job_id, job.result)
        self.storage.update_result(job_id, job.result)
        return self.storage.get_job(job_id)

    def list_jobs(self) -> list[JobRecord]:
        return self.storage.list_jobs()

    def get_job(self, job_id: str) -> JobRecord:
        return self.storage.get_job(job_id)

    def approve(self, job_id: str) -> JobRecord:
        return self.storage.approve(job_id)

    async def rerender_job(self, job_id: str) -> JobRecord:
        """Re-download missing product photos and re-render slide SVGs (fixes broken embeds)."""
        job = self.storage.get_job(job_id)
        if not job.result:
            raise ValueError("job has no result")
        product = job.result.product
        if not any(img.role == "source" and img.asset_id for img in product.images):
            product, image_warnings = await download_product_images(job_id, product, self.storage)
            job.result.product = product
            if image_warnings:
                kept = [
                    w
                    for w in job.result.warnings
                    if "фото" not in w.lower() and "cdn" not in w.lower()
                ]
                job.result.warnings = kept + image_warnings
        job.result = self._render_assets(job_id, job.result)
        self.storage.update_result(job_id, job.result)
        return self.storage.get_job(job_id)
