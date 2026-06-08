from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from identika.models import ProductContext
from identika.storage import Storage

SETTINGS_KEY = "category_templates_json"

FrameStyle = Literal["none", "thin", "accent"]
TitlePosition = Literal["top", "left", "bottom"]
PhotoTreatment = Literal["fit", "expand_square"]


class CategoryTemplate(BaseModel):
    id: str
    name: str
    category: str
    accent_color: str = "#2563eb"
    frame_style: FrameStyle = "thin"
    title_position: TitlePosition = "top"
    photo_treatment: PhotoTreatment = "expand_square"


class CategoryTemplateView(CategoryTemplate):
    is_builtin: bool = False


DEFAULT_TEMPLATES = [
    CategoryTemplate(
        id="cable-default",
        name="Кабель: техно-рамка",
        category="кабель",
        accent_color="#0f766e",
        frame_style="accent",
        title_position="left",
        photo_treatment="expand_square",
    ),
]


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _safe_color(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value
    return "#2563eb"


def list_category_templates(storage: Storage) -> list[CategoryTemplate]:
    raw = storage.get_settings().get(SETTINGS_KEY, "")
    if not raw:
        return list(DEFAULT_TEMPLATES)
    try:
        payload = json.loads(raw)
        templates = [CategoryTemplate.model_validate(item) for item in payload if isinstance(item, dict)]
    except (json.JSONDecodeError, ValueError, TypeError):
        return list(DEFAULT_TEMPLATES)
    return templates or list(DEFAULT_TEMPLATES)


def list_category_template_views(storage: Storage) -> list[CategoryTemplateView]:
    builtin_ids = {item.id for item in DEFAULT_TEMPLATES}
    return [
        CategoryTemplateView(**item.model_dump(), is_builtin=item.id in builtin_ids)
        for item in list_category_templates(storage)
    ]


def save_category_template(storage: Storage, template: CategoryTemplate) -> bool:
    builtin_ids = {_normalize(item.id) for item in DEFAULT_TEMPLATES}
    if _normalize(template.id) in builtin_ids:
        return False
    templates = [item for item in list_category_templates(storage) if item.id != template.id]
    templates.append(template)
    storage.set_settings(
        {SETTINGS_KEY: json.dumps([item.model_dump() for item in templates], ensure_ascii=False)}
    )
    return True


def delete_category_template(storage: Storage, template_id: str) -> bool:
    clean = _normalize(template_id)
    builtin_ids = {_normalize(item.id) for item in DEFAULT_TEMPLATES}
    if clean in builtin_ids:
        return False
    current = list_category_templates(storage)
    templates = [item for item in current if _normalize(item.id) != clean]
    if len(templates) == len(current):
        return False
    storage.set_settings(
        {SETTINGS_KEY: json.dumps([item.model_dump() for item in templates], ensure_ascii=False)}
    )
    return True


def get_category_template(storage: Storage, template_id: str | None) -> CategoryTemplate | None:
    if not template_id:
        return None
    clean = _normalize(template_id)
    return next((item for item in list_category_templates(storage) if _normalize(item.id) == clean), None)


def template_from_form(data: dict[str, str]) -> CategoryTemplate:
    category = _normalize(data.get("category", ""))
    template_id = _normalize(data.get("template_id", "")) or re.sub(r"[^a-z0-9]+", "-", category) or "custom"
    frame_style = data.get("frame_style", "thin")
    title_position = data.get("title_position", "top")
    photo_treatment = data.get("photo_treatment", "expand_square")
    return CategoryTemplate(
        id=template_id[:64],
        name=(data.get("name", "").strip() or category.title() or "Новый шаблон")[:80],
        category=category or "default",
        accent_color=_safe_color(data.get("accent_color", "")),
        frame_style=frame_style if frame_style in {"none", "thin", "accent"} else "thin",
        title_position=title_position if title_position in {"top", "left", "bottom"} else "top",
        photo_treatment=photo_treatment if photo_treatment in {"fit", "expand_square"} else "expand_square",
    )


def find_template_for_product(storage: Storage, product: ProductContext) -> CategoryTemplate | None:
    candidates = [
        _normalize(product.subject_name or ""),
        _normalize(product.title or ""),
    ]
    for template in reversed(list_category_templates(storage)):
        category = _normalize(template.category)
        if not category:
            continue
        for candidate in candidates:
            if candidate == category or category in candidate:
                return template
    return None
