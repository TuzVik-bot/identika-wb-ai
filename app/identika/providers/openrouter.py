from __future__ import annotations

import json
import re

import httpx
from pydantic import BaseModel, Field, ValidationError

from identika.config import settings
from identika.models import CreateJobRequest, GenerationResult
from identika.providers.base import AiProvider
from identika.providers.mock import MockProvider


class _SlideText(BaseModel):
    index: int
    title: str
    subtitle: str = ""
    bullets: list[str] = Field(default_factory=list)


class _RichText(BaseModel):
    index: int
    title: str
    text: str = ""


class _TextPlan(BaseModel):
    slides: list[_SlideText]
    rich_blocks: list[_RichText] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OpenRouterProvider(AiProvider):
    name = "openrouter"

    async def generate(self, request: CreateJobRequest) -> GenerationResult:
        if not settings.openrouter_api_key.strip():
            result = await MockProvider().generate(request)
            result.warnings.append(
                "IDENTIKA_PROVIDER=openrouter but OPENROUTER_API_KEY is empty; using mock generation."
            )
            return result

        result = await MockProvider().generate(request)
        result.provider = self.name
        result.model = f"text={settings.openrouter_text_model}; image={settings.openrouter_image_model}"
        try:
            plan = await self._call_text_model(request)
            self._apply_text_plan(result, plan)
            result.warnings.extend(plan.warnings)
            if settings.enable_ai_images:
                result.warnings.append("Текст сгенерирован OpenRouter; изображения будут запрошены отдельно.")
            else:
                result.warnings.append(
                    "Текст сгенерирован OpenRouter; изображения собираются программным renderer."
                )
        except Exception as exc:
            result.warnings.append(f"OpenRouter text fallback to mock: {type(exc).__name__}")
        if settings.enable_ai_images:
            result.warnings.append(f"Image model: {settings.openrouter_image_model}")
        return result

    async def _call_text_model(self, request: CreateJobRequest) -> _TextPlan:
        payload = {
            "model": settings.openrouter_text_model,
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты маркетолог Wildberries. Верни только JSON без markdown. "
                        "Сделай структуру для 10 слайдов карточки товара: "
                        "1 hero, 2-5 описательные, 6-10 белый фон. "
                        "Русский текст должен быть коротким, честным, без недоказуемых обещаний. "
                        "Схема: {slides:[{index,title,subtitle,bullets}], "
                        "rich_blocks:[{index,title,text}], warnings:[string]}."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "product": request.product.model_dump(mode="json"),
                            "brief": request.brief,
                            "style": request.style,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=self._headers(),
                json=payload,
            )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
        return self._parse_text_plan(str(content))

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://127.0.0.1:8787",
            "X-Title": "Identika WB AI",
        }

    def _parse_text_plan(self, content: str) -> _TextPlan:
        clean = content.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?", "", clean).strip()
            clean = re.sub(r"```$", "", clean).strip()
        try:
            payload = json.loads(clean)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", clean, flags=re.S)
            if not match:
                raise
            payload = json.loads(match.group(0))
        plan = _TextPlan.model_validate(payload)
        if len(plan.slides) != 10:
            raise ValueError("OpenRouter text plan must contain exactly 10 slides")
        return plan

    def _apply_text_plan(self, result: GenerationResult, plan: _TextPlan) -> None:
        by_index = {slide.index: slide for slide in plan.slides}
        for slide in result.slides:
            incoming = by_index.get(slide.index)
            if not incoming:
                continue
            slide.title = incoming.title.strip()
            slide.subtitle = incoming.subtitle.strip()
            slide.bullets = [item.strip() for item in incoming.bullets if item.strip()]
            for block in slide.text_blocks:
                if block.kind == "title":
                    block.text = slide.title
                elif block.kind == "subtitle":
                    block.text = slide.subtitle
        rich_by_index = {block.index: block for block in plan.rich_blocks}
        for block in result.rich.blocks:
            incoming = rich_by_index.get(block.index)
            if incoming:
                block.title = incoming.title.strip()
                block.text = incoming.text.strip()


def get_provider() -> AiProvider:
    if settings.effective_provider == "openrouter":
        return OpenRouterProvider()
    return MockProvider()
