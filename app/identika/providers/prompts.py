from __future__ import annotations

from identika.models import CreateJobRequest, GenerationResult, SlideSpec

NO_TEXT_IMAGE_RULE = (
    "STRICT: absolutely NO text, NO letters, NO words, NO numbers, NO typography, "
    "NO labels, NO watermarks, NO logos with readable text, NO bullet lists, NO captions, "
    "NO infographic copy, NO marketing slogans, NO price tags, NO UI chrome. "
    "All titles, subtitles, and bullet points are added later in a separate SVG text layer — "
    "the image must be purely visual."
)

KIT_CONTENTS_IMAGE_RULE = (
    "For slide 10 (kit contents), schematic callout shapes/icons are allowed, "
    "but still avoid any readable text in the generated image."
)

WHITE_BG_ANGLE_TITLES = (
    "Вид сверху",
    "Вид сбоку",
    "Вид снизу",
    "Деталь товара",
    "Комплект поставки",
)

WHITE_BG_ANGLE_SUBTITLES = (
    "Фото на белом фоне для галереи WB",
    "Боковой ракурс на белом фоне",
    "Нижний ракурс на белом фоне",
    "Крупный план детали",
    "Инфографика состава комплекта",
)


def build_visual_prompt(slide: SlideSpec, request: CreateJobRequest) -> str:
    product = request.product
    title = (product.title or "product")[:80]
    subject = (product.subject_name or "marketplace product")[:60]

    if slide.role == "hero":
        return (
            f"Wildberries hero slide background, {subject}. "
            f"Product: {title}. Clean premium marketplace infographic BACKGROUND only. "
            "Product centered in lower 60% of frame. Leave top 40% empty negative space "
            "for text overlay. Soft studio lighting, minimal props. "
            f"{NO_TEXT_IMAGE_RULE}"
        )

    if slide.role == "description":
        return (
            f"Wildberries description slide background, {subject}. "
            f"Product: {title}. Lifestyle or studio scene BACKGROUND only — no infographic text. "
            "Product centered in lower 55%, top 45% clean empty area for title overlay. "
            "Bottom area uncluttered for bullet text overlay. "
            f"{NO_TEXT_IMAGE_RULE}"
        )

    # white_background — only used when no real product photos are available
    angle_idx = max(0, min(slide.index - 6, len(WHITE_BG_ANGLE_TITLES) - 1))
    angle = WHITE_BG_ANGLE_TITLES[angle_idx].lower()
    extra_rule = KIT_CONTENTS_IMAGE_RULE if slide.index == 10 else NO_TEXT_IMAGE_RULE
    return (
        f"Professional e-commerce product photography on pure white background (#FFFFFF). "
        f"Product: {title}. {angle} angle, centered, studio lighting, no props, no shadows on background. "
        "Single product only, marketplace catalog style. For kit slide use separate item grouping zones. "
        f"{extra_rule}"
    )


def apply_visual_prompts(result: GenerationResult, request: CreateJobRequest) -> None:
    for slide in result.slides:
        slide.visual_prompt = build_visual_prompt(slide, request)
        if slide.role == "white_background" and slide.index >= 6:
            angle_idx = slide.index - 6
            if angle_idx < len(WHITE_BG_ANGLE_TITLES):
                slide.title = WHITE_BG_ANGLE_TITLES[angle_idx]
                slide.subtitle = WHITE_BG_ANGLE_SUBTITLES[angle_idx]
                if slide.index < 10:
                    slide.bullets = []
                elif not slide.bullets:
                    slide.bullets = ["Основной товар", "Аксессуары", "Документация"]


def build_image_model_user_prompt(
    slide: SlideSpec,
    visual_prompt: str,
    request: CreateJobRequest,
) -> str:
    product_title = (request.product.title or "product")[:80]
    base = (
        "Generate a single portrait product image for a Wildberries marketplace slide. "
        "Exact dimensions: 900x1200 pixels, 3:4 aspect ratio. "
        "Fill the entire canvas edge-to-edge; do not add borders or letterboxing bars. "
        f"{NO_TEXT_IMAGE_RULE} "
        "English composition only in the image — no Cyrillic, no Latin marketing copy. "
    )

    if slide.role == "hero":
        role_hint = (
            "Role: HERO slide background. Show product in an attractive scene or clean studio. "
            "Keep top 40% of the frame empty (plain/light) for title text added later in SVG. "
            "Center the product in the lower portion."
        )
    elif slide.role == "description":
        role_hint = (
            "Role: DESCRIPTION slide background. Product scene or soft studio backdrop ONLY. "
            "Do NOT render titles, subtitles, bullet points, feature lists, or checkmarks in the image. "
            "Keep top 45% plain/light/empty for title overlay. "
            "Keep bottom 25% uncluttered for bullet text overlay added programmatically."
        )
    else:
        role_hint = (
            "Role: WHITE BACKGROUND product photo. Pure white (#FFFFFF) background, "
            "professional catalog photography, product centered, no props, no text."
        )
        if slide.index == 10:
            role_hint = (
                "Role: KIT CONTENTS infographic base image. White background, grouped product set, "
                "clean composition zones for later SVG callouts. Avoid readable text."
            )

    ref_hint = (
        "The attached reference photo is the exact product — match its shape, color, "
        "and category; do not substitute a different product type."
    )
    return (
        f"{base}{role_hint} {ref_hint} "
        f"Creative direction: {visual_prompt}. "
        f"Brief: {request.brief or 'marketplace product card'}. "
        f"Product name (do not render as text): {product_title}."
    )


def should_skip_ai_image(slide: SlideSpec, source_asset_ids: list[str]) -> bool:
    """Skip AI when real product photos will be used in the programmatic renderer."""
    if not source_asset_ids:
        return False
    if slide.role == "white_background":
        return True
    if slide.role == "description":
        return True
    return False


OPENROUTER_TEXT_SYSTEM_PROMPT = (
    "Ты маркетолог Wildberries. Верни только JSON без markdown. "
    "Структура карточки: слайд 1 — hero; слайды 2–5 — описательные (description); "
    "слайды 6–10 — белый фон (white_background), только фото без маркетингового текста. "
    "Русский текст: короткий, честный, единый тон, без недоказуемых обещаний. "
    "Заголовок до 50 символов, подзаголовок до 70, bullets — максимум 3 коротких пункта на description-слайдах. "
    "На слайдах 6–9: title = ракурс фото (Вид сверху, Вид сбоку, Вид снизу, Деталь товара), "
    "subtitle = кратко про ракурс, bullets = пустой массив []. "
    "Слайд 10 строго: title = 'Комплект поставки', subtitle про состав комплекта, bullets 2-4 коротких элемента комплекта. "
    "Поле warnings — только служебные замечания для оператора, не копируй их в title/subtitle/bullets. "
    "Схема: {slides:[{index,title,subtitle,bullets}], rich_blocks:[{index,title,text}], warnings:[string]}."
)
