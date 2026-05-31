from __future__ import annotations

from identika.models import (
    CreateJobRequest,
    GenerationResult,
    RichBlock,
    RichPackage,
    SlideSpec,
    TextBlock,
)
from identika.providers.base import AiProvider


class MockProvider(AiProvider):
    name = "mock"

    async def generate(self, request: CreateJobRequest) -> GenerationResult:
        product = request.product
        title = product.title or "Товар WB"
        subject = product.subject_name or "товар"
        base_bullets = [
            "Готово для WB",
            "Чёткая подача преимуществ",
            "Аккуратная инфографика",
            "Ручная проверка перед загрузкой",
        ]
        slides: list[SlideSpec] = []
        for idx in range(1, 11):
            if idx == 1:
                role = "hero"
                slide_title = title[:60]
                subtitle = "Кратко о главных преимуществах товара"
                bullets = base_bullets[:4]
            elif idx <= 5:
                role = "description"
                slide_title = f"{title[:42]}: преимущество {idx - 1}"
                subtitle = f"Описание свойства для категории: {subject}"
                bullets = [
                    "Показываем пользу без лишнего текста",
                    "Оставляем место для визуального акцента",
                    "Текст можно отредактировать перед approve",
                ]
            else:
                role = "white_background"
                slide_title = f"{title[:46]} на белом фоне"
                subtitle = "Чистое товарное фото для галереи"
                bullets = []
            slides.append(
                SlideSpec(
                    index=idx,
                    role=role,  # type: ignore[arg-type]
                    title=slide_title,
                    subtitle=subtitle,
                    bullets=bullets,
                    visual_prompt=f"WB product slide {idx}, clean marketplace style, {subject}",
                    text_blocks=[
                        TextBlock(kind="title", text=slide_title),
                        TextBlock(kind="subtitle", text=subtitle, y=0.24, size=28),
                    ],
                )
            )
        rich_blocks = [
            RichBlock(index=i, title=f"Блок {i}: {slides[i-1].title}", text=slides[i-1].subtitle)
            for i in range(1, 11)
        ]
        return GenerationResult(
            provider=self.name,
            model="mock-layout-v1",
            product=product,
            slides=slides,
            rich=RichPackage(blocks=rich_blocks),
            warnings=["Mock-режим: реальные AI-изображения не запрашивались."],
        )
