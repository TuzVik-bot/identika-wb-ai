from __future__ import annotations

import json
import re

import httpx
from pydantic import BaseModel, Field, ValidationError

from identika.config import EffectiveSettings, settings
from identika.models import CreateJobRequest, GenerationResult
from identika.providers.base import AiProvider
from identika.providers.mock import MockProvider
from identika.providers.prompts import (
    OPENROUTER_TEXT_SYSTEM_PROMPT,
    WHITE_BG_ANGLE_SUBTITLES,
    WHITE_BG_ANGLE_TITLES,
    apply_visual_prompts,
)


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

    async def generate(
        self,
        request: CreateJobRequest,
        eff: EffectiveSettings | None = None,
    ) -> GenerationResult:
        eff = eff or EffectiveSettings.resolve()
        if not eff.openrouter_api_key.strip():
            result = await MockProvider().generate(request)
            result.warnings = [
                "IDENTIKA_PROVIDER=openrouter but OPENROUTER_API_KEY is empty; using mock generation."
            ]
            result.info = []
            return result

        result = await MockProvider().generate(request)
        result.warnings = []
        result.info = []
        result.provider = self.name
        result.model = f"text={eff.openrouter_text_model}; image={eff.openrouter_image_model}"
        try:
            plan = await self._call_text_model(request, eff)
            self._apply_text_plan(result, plan)
            apply_visual_prompts(result, request)
            if eff.enable_ai_images:
                result.info.append("Текст сгенерирован OpenRouter; изображения будут запрошены отдельно.")
            else:
                result.info.append(
                    "Текст сгенерирован OpenRouter; изображения собираются программным renderer."
                )
        except Exception as exc:
            result.warnings.append(f"OpenRouter text fallback to mock: {type(exc).__name__}")
        if eff.enable_ai_images:
            result.info.append(f"Image model: {eff.openrouter_image_model}")
        return result

    async def _call_text_model(self, request: CreateJobRequest, eff: EffectiveSettings) -> _TextPlan:
        payload = {
            "model": eff.openrouter_text_model,
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": OPENROUTER_TEXT_SYSTEM_PROMPT,
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
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=self._headers(eff),
                json=payload,
            )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
        return self._parse_text_plan(str(content))

    def _headers(self, eff: EffectiveSettings) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {eff.openrouter_api_key}",
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
        self._normalize_white_background_contract(result)
        rich_by_index = {block.index: block for block in plan.rich_blocks}
        for block in result.rich.blocks:
            incoming = rich_by_index.get(block.index)
            if incoming:
                block.title = incoming.title.strip()
                block.text = incoming.text.strip()
        # LLM product disclaimers stay out of service warnings (shown only in info if needed).
        if plan.warnings:
            for item in plan.warnings:
                clean = item.strip()
                if clean:
                    result.info.append(f"Заметка ИИ: {clean}")

    def _normalize_white_background_contract(self, result: GenerationResult) -> None:
        for slide in result.slides:
            if slide.role != "white_background" or slide.index < 6:
                continue
            angle_idx = slide.index - 6
            if angle_idx >= len(WHITE_BG_ANGLE_TITLES):
                continue
            slide.title = WHITE_BG_ANGLE_TITLES[angle_idx]
            slide.subtitle = WHITE_BG_ANGLE_SUBTITLES[angle_idx]
            if slide.index < 10:
                slide.bullets = []
            else:
                slide.bullets = self._normalize_kit_bullets(result, slide.bullets)
            for block in slide.text_blocks:
                if block.kind == "title":
                    block.text = slide.title
                elif block.kind == "subtitle":
                    block.text = slide.subtitle

    def _normalize_kit_bullets(
        self,
        result: GenerationResult,
        bullets: list[str],
    ) -> list[str]:
        normalized = [item.strip() for item in bullets if item.strip()][:4]
        fallback_pool = [
            *[str(key).strip() for key in result.product.characteristics.keys() if str(key).strip()],
            "Основной товар",
            "Комплектующие",
            "Инструкция",
            "Упаковка",
        ]
        for item in fallback_pool:
            if len(normalized) >= 2:
                break
            if item and item not in normalized:
                normalized.append(item)
        return normalized[:4]


def get_provider(storage=None) -> AiProvider:
    eff = EffectiveSettings.resolve(storage)
    if eff.effective_provider == "openrouter":
        return OpenRouterProvider()
    return MockProvider()
