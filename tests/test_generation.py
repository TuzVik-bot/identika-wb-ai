from __future__ import annotations

import asyncio
import io
import zipfile

from PIL import Image

from identika.config import settings
from identika.models import CreateJobRequest, ProductContext, ProductImage, ResultTextPatch
from identika.providers.mock import MockProvider
from identika.services.category_templates import CategoryTemplate, save_category_template
from identika.services.jobs import JobService
from identika.storage import Storage


def product() -> ProductContext:
    settings.identika_provider = "mock"
    return ProductContext(
        store_slug="test",
        sku_id=7,
        nm_id=7001,
        title="Ночник-проектор звёздного неба",
        subject_name="Дом и интерьер",
        characteristics={"Питание": "USB"},
    )


def test_mock_provider_returns_exactly_ten_slides_with_roles() -> None:
    result = asyncio.run(MockProvider().generate(CreateJobRequest(product=product())))

    assert len(result.slides) == 10
    assert result.slides[0].role == "hero"
    assert [slide.role for slide in result.slides[1:5]] == ["description"] * 4
    assert [slide.role for slide in result.slides[5:]] == ["white_background"] * 5
    assert len(result.rich.blocks) == 10


def test_render_slide_svg_uses_full_bleed_background() -> None:
    from identika.models import SlideSpec
    from identika.services.rendering import render_slide_svg

    slide = SlideSpec(index=1, role="hero", title="Тест", subtitle="Подзаголовок")
    svg = render_slide_svg(
        slide,
        background_image_href="/identika/v1/assets/bg123",
    ).decode("utf-8")
    assert '<image href="/identika/v1/assets/bg123"' in svg
    assert 'x="0" y="0" width="900" height="1200"' in svg
    assert 'viewBox="0 0 900 1200"' in svg
    assert 'preserveAspectRatio="xMidYMid meet"' in svg
    assert 'width="560" height="320"' not in svg
    assert "ТОВАР" not in svg
    assert "data:image" not in svg


def test_render_slide_svg_white_background_centers_product() -> None:
    from identika.models import SlideSpec
    from identika.services.rendering import render_slide_svg

    slide = SlideSpec(index=6, role="white_background", title="Фото", subtitle="")
    svg = render_slide_svg(
        slide,
        source_image_href="/identika/v1/assets/product.png",
    ).decode("utf-8")
    assert 'x="80" y="120" width="740" height="960"' in svg
    assert "Слайд 06" not in svg
    assert 'fill="#ffffff"' in svg


def test_render_slide_svg_kit_contents_infographic_uses_callout_layout() -> None:
    from identika.models import SlideSpec
    from identika.services.rendering import render_slide_svg

    slide = SlideSpec(
        index=10,
        role="white_background",
        title="Комплект поставки",
        subtitle="Инфографика состава комплекта",
        bullets=["Товар", "Кабель", "Упаковка", "Инструкция", "Гарантия"],
    )
    svg = render_slide_svg(
        slide,
        source_image_href="/identika/v1/assets/product.png",
    ).decode("utf-8")
    assert "Комплект поставки" in svg
    assert '<image href="/identika/v1/assets/product.png"' in svg
    assert 'x="70" y="190" width="380" height="500"' in svg
    assert 'x="80" y="120" width="740" height="960"' not in svg
    assert "Товар" in svg
    assert "Инструкция" in svg
    assert "Гарантия" not in svg


def test_slide_10_visual_prompt_keeps_no_text_rule_and_callout_hint() -> None:
    from identika.models import SlideSpec
    from identika.providers.prompts import build_visual_prompt

    slide = SlideSpec(
        index=10,
        role="white_background",
        title="Комплект поставки",
        subtitle="Инфографика состава комплекта",
    )
    prompt = build_visual_prompt(slide, CreateJobRequest(product=product()))
    assert "STRICT: absolutely NO text" in prompt
    assert "schematic callout shapes/icons are allowed" in prompt
    assert "separate item grouping zones" in prompt


def test_render_slide_svg_has_wb_dimensions() -> None:
    from identika.models import SlideSpec
    from identika.services.rendering import render_slide_svg

    slide = SlideSpec(index=2, role="description", title="A", subtitle="B")
    svg = render_slide_svg(slide).decode("utf-8")
    assert 'viewBox="0 0 900 1200"' in svg
    assert 'width="100%" height="100%"' in svg
    assert 'preserveAspectRatio="xMidYMid meet"' in svg


def test_job_service_slide_svg_references_product_image_not_placeholder(tmp_path) -> None:
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "530000000a49444154789c6260000000020001e221bc330000000049454e44ae426082"
    )
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = storage.create_job(
        CreateJobRequest(product=ProductContext(title="USB хаб")).model_dump(mode="json")
    )
    source_id = storage.add_asset(job.id, "product.png", png, "image/png")
    product = ProductContext(
        title="USB хаб",
        images=[ProductImage(asset_id=source_id, role="source")],
    )
    result = asyncio.run(MockProvider().generate(CreateJobRequest(product=product)))
    result.product = product
    rendered = service._render_assets(job.id, result)
    slide_path, _ = storage.get_asset(rendered.slides[0].asset_id)
    svg = slide_path.read_text(encoding="utf-8")
    assert "<image" in svg
    assert "ТОВАР" not in svg
    assert f"/v1/assets/{source_id}" in svg
    assert len(svg) < 200_000

    export_path, _ = storage.get_asset(rendered.export_asset_id)
    with zipfile.ZipFile(export_path) as zf:
        exported_image = Image.open(io.BytesIO(zf.read("slides/slide_01.png")))
    assert exported_image.format == "PNG"
    assert exported_image.size == (900, 1200)


def test_job_service_exports_assets_pdf_manifest_and_zip(tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)

    job = asyncio.run(
        service.create_job(
            CreateJobRequest(product=product(), allow_generate_without_photos=True)
        )
    )

    assert job.status == "succeeded"
    assert job.result is not None
    assert len(job.result.slides) == 10
    assert job.result.rich.pdf_asset_id
    assert job.result.rich.html_asset_id
    assert job.result.export_asset_id

    export_path, media_type = storage.get_asset(job.result.export_asset_id)
    assert media_type == "application/zip"
    with zipfile.ZipFile(export_path) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "rich/preview.pdf" in names
        assert "rich/preview.html" not in names
        assert "rich/block_01.png" in names
        assert "slides/slide_01.png" in names
        assert "slides/slide_10.png" in names
        assert not any(n.startswith("slides/") and n.endswith(".svg") for n in names)
        slide_image = Image.open(io.BytesIO(zf.read("slides/slide_01.png")))
        assert slide_image.format == "PNG"
        assert slide_image.size == (900, 1200)
        rich_image = Image.open(io.BytesIO(zf.read("rich/block_01.png")))
        assert rich_image.size == (1440, 900)


def test_rich_zip_exports_png_blocks_not_html(tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)

    job = asyncio.run(
        service.create_job(
            CreateJobRequest(product=product(), allow_generate_without_photos=True)
        )
    )

    assert job.result is not None
    assert job.result.rich.zip_asset_id

    rich_zip_path, _ = storage.get_asset(job.result.rich.zip_asset_id)
    with zipfile.ZipFile(rich_zip_path) as zf:
        names = set(zf.namelist())
        assert "rich/preview.html" not in names
        assert "rich/block_01.png" in names
        assert not any(n.startswith("rich/block_") and n.endswith(".html") for n in names)
        assert not any(n.startswith("rich/block_") and n.endswith(".svg") for n in names)
        image = Image.open(io.BytesIO(zf.read("rich/block_01.png")))
        assert image.format == "PNG"
        assert image.size == (1440, 900)


def test_category_template_applies_to_export_and_square_photo_is_preserved(tmp_path) -> None:
    square_buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "#22c55e").save(square_buffer, format="PNG")
    square_png = square_buffer.getvalue()
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    save_category_template(
        storage,
        CategoryTemplate(
            id="cable-test",
            name="Кабель тест",
            category="кабель",
            accent_color="#0f766e",
            frame_style="accent",
            title_position="left",
            photo_treatment="expand_square",
        ),
    )
    service = JobService(storage)
    job = storage.create_job(
        CreateJobRequest(product=ProductContext(title="USB кабель")).model_dump(mode="json")
    )
    source_id = storage.add_asset(job.id, "square.png", square_png, "image/png")
    source_path, _ = storage.get_asset(source_id)
    original_bytes = source_path.read_bytes()
    result = asyncio.run(
        MockProvider().generate(
            CreateJobRequest(
                product=ProductContext(
                    title="USB кабель",
                    subject_name="Кабель",
                    images=[ProductImage(asset_id=source_id, role="source")],
                )
            )
        )
    )
    result.product = ProductContext(
        title="USB кабель",
        subject_name="Кабель",
        images=[ProductImage(asset_id=source_id, role="source")],
    )

    rendered = service._render_assets(job.id, result)

    assert "Шаблон категории: Кабель тест" in rendered.info
    assert source_path.read_bytes() == original_bytes
    export_path, _ = storage.get_asset(rendered.export_asset_id)
    with zipfile.ZipFile(export_path) as zf:
        image = Image.open(io.BytesIO(zf.read("slides/slide_06.png")))
    assert image.format == "PNG"
    assert image.size == (900, 1200)


def test_category_template_id_overrides_auto_category_match(tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    save_category_template(
        storage,
        CategoryTemplate(
            id="manual-template",
            name="Ручной шаблон",
            category="чехол",
            accent_color="#2563eb",
            frame_style="thin",
            title_position="bottom",
            photo_treatment="fit",
        ),
    )
    service = JobService(storage)
    job = asyncio.run(
        service.create_job(
            CreateJobRequest(
                product=product().model_copy(update={"subject_name": "Кабель"}),
                allow_generate_without_photos=True,
                category_template_id="manual-template",
            )
        )
    )

    assert job.result is not None
    assert job.result.category_template_id == "manual-template"
    assert "Шаблон категории: Ручной шаблон" in job.result.info
    assert "Шаблон категории: Кабель: техно-рамка" not in job.result.info

    export_path, _ = storage.get_asset(job.result.export_asset_id)
    with zipfile.ZipFile(export_path) as zf:
        names = set(zf.namelist())
        assert "slides/slide_01.png" in names
        assert "slides/slide_10.png" in names
        assert "rich/block_01.png" in names
        assert "rich/preview.html" not in names
        assert not any(n.startswith("slides/") and n.endswith(".svg") for n in names)
        slide = Image.open(io.BytesIO(zf.read("slides/slide_01.png")))
        rich = Image.open(io.BytesIO(zf.read("rich/block_01.png")))
    assert slide.format == "PNG"
    assert slide.size == (900, 1200)
    assert rich.format == "PNG"
    assert rich.size == (1440, 900)


def test_approve_only_after_success(tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = asyncio.run(
        service.create_job(
            CreateJobRequest(product=product(), allow_generate_without_photos=True)
        )
    )

    approved = service.approve(job.id)

    assert approved.status == "approved"
    assert approved.approved_at is not None
    assert approved.result is not None
    assert approved.result.quality_mode == "final"
    assert approved.result.rich.zip_asset_id


def test_result_does_not_include_known_secret_fields(tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = asyncio.run(
        service.create_job(
            CreateJobRequest(product=product(), allow_generate_without_photos=True)
        )
    )

    dumped = job.result.model_dump_json() if job.result else ""

    assert "wb_api_token" not in dumped
    assert "b2b_client_secret" not in dumped
    assert "OPENROUTER_API_KEY" not in dumped


def test_clear_slide_image_sets_flag_and_renders_placeholder(tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = asyncio.run(
        service.create_job(
            CreateJobRequest(product=product(), allow_generate_without_photos=True)
        )
    )
    updated = service.clear_slide_image(job.id, 1)
    assert updated.result is not None
    slide = updated.result.slides[0]
    assert slide.image_cleared is True
    slide_path, _ = storage.get_asset(slide.asset_id)
    assert "Загрузите фото товара" in slide_path.read_text(encoding="utf-8")


def test_patch_result_text_updates_manifest_export(tmp_path) -> None:
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = asyncio.run(
        service.create_job(
            CreateJobRequest(product=product(), allow_generate_without_photos=True)
        )
    )

    updated = service.patch_result_text(
        job.id,
        ResultTextPatch(
            slides=[{"index": 1, "title": "Новый заголовок", "subtitle": "Новый подзаголовок"}],
            rich_blocks=[{"index": 1, "title": "Новый rich", "text": "Новый текст"}],
        ),
    )

    assert updated.result is not None
    assert updated.result.slides[0].title == "Новый заголовок"
    assert updated.result.rich.blocks[0].title == "Новый rich"

    export_path, _ = storage.get_asset(updated.result.export_asset_id)
    with zipfile.ZipFile(export_path) as zf:
        manifest = zf.read("manifest.json").decode("utf-8")
    assert "Новый заголовок" in manifest


def test_white_background_slides_use_product_photo_not_ai_background(tmp_path) -> None:
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "530000000a49444154789c6260000000020001e221bc330000000049454e44ae426082"
    )
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = storage.create_job(
        CreateJobRequest(product=ProductContext(title="Мышь")).model_dump(mode="json")
    )
    source_id = storage.add_asset(job.id, "product.png", png, "image/png")
    product = ProductContext(
        title="Мышь",
        images=[ProductImage(asset_id=source_id, role="source")],
    )
    result = asyncio.run(MockProvider().generate(CreateJobRequest(product=product)))
    result.product = product
    for slide in result.slides[5:]:
        slide.background_asset_id = "fake-ai-bg"
    rendered = service._render_assets(job.id, result)
    slide6_path, _ = storage.get_asset(rendered.slides[5].asset_id)
    svg = slide6_path.read_text(encoding="utf-8")
    assert f"/v1/assets/{source_id}" in svg
    assert "fake-ai-bg" not in svg
    assert 'fill="#ffffff"' in svg
    assert "<image" in svg


def test_description_slides_use_primary_photo_not_gallery_rotation(tmp_path) -> None:
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "530000000a49444154789c6260000000020001e221bc330000000049454e44ae426082"
    )
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = storage.create_job(
        CreateJobRequest(product=ProductContext(title="Ночник")).model_dump(mode="json")
    )
    primary_id = storage.add_asset(job.id, "primary.png", png, "image/png")
    wrong_id = storage.add_asset(job.id, "wrong.png", png, "image/png")
    product = ProductContext(
        title="Ночник",
        images=[
            ProductImage(asset_id=primary_id, role="source"),
            ProductImage(asset_id=wrong_id, role="source"),
        ],
    )
    result = asyncio.run(MockProvider().generate(CreateJobRequest(product=product)))
    result.product = product
    rendered = service._render_assets(job.id, result)
    for slide in rendered.slides[1:5]:
        slide_path, _ = storage.get_asset(slide.asset_id)
        svg = slide_path.read_text(encoding="utf-8")
        assert f"/v1/assets/{primary_id}" in svg
        assert f"/v1/assets/{wrong_id}" not in svg


def test_description_slides_prefer_composite_over_ai_background(tmp_path) -> None:
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "530000000a49444154789c6260000000020001e221bc330000000049454e44ae426082"
    )
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = storage.create_job(
        CreateJobRequest(product=ProductContext(title="Мышь")).model_dump(mode="json")
    )
    source_id = storage.add_asset(job.id, "product.png", png, "image/png")
    bg_id = storage.add_asset(job.id, "bg.png", png, "image/png")
    product = ProductContext(
        title="Мышь",
        images=[ProductImage(asset_id=source_id, role="source")],
    )
    result = asyncio.run(MockProvider().generate(CreateJobRequest(product=product)))
    result.product = product
    result.slides[1].background_asset_id = bg_id
    rendered = service._render_assets(job.id, result)
    slide2_path, _ = storage.get_asset(rendered.slides[1].asset_id)
    svg = slide2_path.read_text(encoding="utf-8")
    assert f"/v1/assets/{source_id}" in svg
    assert f"/v1/assets/{bg_id}" not in svg
    assert 'width="560" height="320"' in svg


def test_white_background_falls_back_to_ai_background_without_sources(tmp_path) -> None:
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "530000000a49444154789c6260000000020001e221bc330000000049454e44ae426082"
    )
    storage = Storage(db_path=tmp_path / "identika.sqlite", assets_dir=tmp_path / "assets")
    service = JobService(storage)
    job = storage.create_job(
        CreateJobRequest(product=ProductContext(title="Мышь")).model_dump(mode="json")
    )
    bg_id = storage.add_asset(job.id, "white_bg.png", png, "image/png")
    result = asyncio.run(MockProvider().generate(CreateJobRequest(product=ProductContext(title="Мышь"))))
    result.slides[5].background_asset_id = bg_id
    rendered = service._render_assets(job.id, result)
    slide6_path, _ = storage.get_asset(rendered.slides[5].asset_id)
    svg = slide6_path.read_text(encoding="utf-8")
    assert f"/v1/assets/{bg_id}" in svg
    assert "ТОВАР" not in svg


    from identika.providers.prompts import build_visual_prompt, should_skip_ai_image

    request = CreateJobRequest(product=product())
    result = asyncio.run(MockProvider().generate(request))
    hero_prompt = build_visual_prompt(result.slides[0], request)
    desc_prompt = build_visual_prompt(result.slides[2], request)
    white_prompt = build_visual_prompt(result.slides[5], request)
    assert "NO text" in hero_prompt
    assert "NO text" in desc_prompt
    assert "white background" in white_prompt.lower()
    assert result.slides[5].title == "Вид сверху"
    assert should_skip_ai_image(result.slides[5], ["asset-1"])
    assert not should_skip_ai_image(result.slides[0], ["asset-1"])
