from __future__ import annotations

import base64
import json
import re

import httpx

from identika.config import settings
from identika.models import CreateJobRequest, GenerationResult
from identika.storage import Storage


async def generate_slide_images(
    job_id: str,
    request: CreateJobRequest,
    result: GenerationResult,
    storage: Storage,
) -> GenerationResult:
    if not settings.openrouter_api_key:
        result.warnings.append("AI images skipped: OPENROUTER_API_KEY is missing")
        return result

    source_refs = [
        image.asset_id
        for image in result.product.images
        if image.role == "source" and image.asset_id
    ]
    failures = 0
    for slide in result.slides:
        try:
            image_bytes = await _call_image_model(slide.visual_prompt, request, source_refs, storage)
            asset_id = storage.add_asset(
                job_id,
                f"slide_{slide.index:02d}_bg.png",
                image_bytes,
                "image/png",
            )
            slide.background_asset_id = asset_id
        except Exception as exc:
            failures += 1
            result.warnings.append(
                f"Slide {slide.index}: AI image fallback to programmatic SVG ({type(exc).__name__})"
            )
    if failures == 0:
        result.warnings.append("Slide backgrounds generated via OpenRouter image model.")
    elif failures < len(result.slides):
        result.warnings.append(f"Partial AI image success: {len(result.slides) - failures}/{len(result.slides)} slides.")
    return result


async def _call_image_model(
    visual_prompt: str,
    request: CreateJobRequest,
    source_refs: list[str],
    storage: Storage,
) -> bytes:
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "Generate a marketplace product slide background image. "
                f"Prompt: {visual_prompt}. Brief: {request.brief}. "
                f"Product: {request.product.title}."
            ),
        }
    ]
    for asset_id in source_refs[:1]:
        path, media_type = storage.get_asset(asset_id)
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{encoded}"},
            }
        )

    payload = {
        "model": settings.openrouter_image_model,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["image", "text"],
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=_headers(),
            json=payload,
        )
    response.raise_for_status()
    data = response.json()
    message = data["choices"][0]["message"]
    images = message.get("images") or []
    if images:
        url = images[0].get("image_url", {}).get("url") or images[0].get("url", "")
        return _decode_image_url(str(url))
    content_value = message.get("content")
    if isinstance(content_value, list):
        for part in content_value:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                return _decode_image_url(str(url))
    if isinstance(content_value, str) and "base64," in content_value:
        return _decode_image_url(content_value)
    raise ValueError("OpenRouter image response did not include image data")


def _decode_image_url(url: str) -> bytes:
    if url.startswith("data:"):
        match = re.search(r"base64,(.+)$", url, flags=re.S)
        if not match:
            raise ValueError("invalid data URI in image response")
        return base64.b64decode(match.group(1))
    raise ValueError("remote image URLs are not supported in image response")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://127.0.0.1:8787",
        "X-Title": "Identika WB AI",
    }
