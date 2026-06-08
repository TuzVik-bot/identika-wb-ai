from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

SCHEMA_VERSION = "1.0"
SLIDE_COUNT = 10

SlideRole = Literal["hero", "description", "white_background"]
QualityMode = Literal["preview", "final"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "approved"]


class ProductImage(BaseModel):
    url: str | None = None
    asset_id: str | None = None
    role: str = "source"
    alt: str = ""


class ProductContext(BaseModel):
    schema_version: str = SCHEMA_VERSION
    account_id: int | None = None
    store_slug: str = "default"
    sku_id: int | None = None
    nm_id: int | None = None
    vendor_code: str | None = None
    barcode: str | None = None
    title: str = "Товар WB"
    brand: str | None = None
    subject_name: str | None = None
    description: str | None = None
    characteristics: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    price: float | None = None
    discount: int | None = None
    final_price: float | None = None
    dimensions: dict[str, float | None] = Field(default_factory=dict)
    images: list[ProductImage] = Field(default_factory=list)
    source_url: HttpUrl | None = None


class TextBlock(BaseModel):
    kind: str
    text: str
    x: float = 0.08
    y: float = 0.12
    width: float = 0.84
    align: Literal["left", "center", "right"] = "left"
    size: int = 42


class SlideSpec(BaseModel):
    index: int = Field(ge=1, le=SLIDE_COUNT)
    role: SlideRole
    title: str
    subtitle: str = ""
    bullets: list[str] = Field(default_factory=list)
    visual_prompt: str = ""
    text_blocks: list[TextBlock] = Field(default_factory=list)
    asset_id: str | None = None
    background_asset_id: str | None = None
    image_cleared: bool = False
    width: int = 900
    height: int = 1200


class RichBlock(BaseModel):
    index: int
    title: str
    text: str
    asset_id: str | None = None


class RichPackage(BaseModel):
    cover_asset_id: str | None = None
    html_asset_id: str | None = None
    pdf_asset_id: str | None = None
    zip_asset_id: str | None = None
    blocks: list[RichBlock] = Field(default_factory=list)


class GenerationResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    prompt_version: str = "mock-v1"
    provider: str = "mock"
    model: str = "mock"
    product: ProductContext
    slides: list[SlideSpec]
    rich: RichPackage
    warnings: list[str] = Field(default_factory=list)
    info: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    quality_mode: QualityMode = "preview"
    export_asset_id: str | None = None
    category_template_id: str | None = None


class CreateJobRequest(BaseModel):
    product: ProductContext
    brief: str = ""
    style: str = "marketplace-clean"
    outputs: list[str] = Field(default_factory=lambda: ["wb_10_slides", "rich_package"])
    source_image_asset_ids: list[str] = Field(default_factory=list)
    allow_generate_without_photos: bool = False
    category_template_id: str | None = None


class SlideTextUpdate(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    bullets: list[str] | None = None


class SlideTextPatch(SlideTextUpdate):
    index: int = Field(ge=1, le=SLIDE_COUNT)


class RichBlockTextPatch(BaseModel):
    index: int = Field(ge=1, le=SLIDE_COUNT)
    title: str | None = None
    text: str | None = None


class ResultTextPatch(BaseModel):
    slides: list[SlideTextPatch] = Field(default_factory=list)
    rich_blocks: list[RichBlockTextPatch] = Field(default_factory=list)


class JobRecord(BaseModel):
    id: str
    status: JobStatus
    product_title: str
    store_slug: str
    nm_id: int | None = None
    sku_id: int | None = None
    error: str | None = None
    result: GenerationResult | None = None
    approved_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
