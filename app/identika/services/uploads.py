from __future__ import annotations

import uuid
from io import BytesIO
from typing import TYPE_CHECKING

from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

if TYPE_CHECKING:
    from identika.storage import Storage

MAX_SOURCE_IMAGES = 4
MAX_SOURCE_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png"}
ALLOWED_IMAGE_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png"}


def new_upload_session_id() -> str:
    return uuid.uuid4().hex


def _detect_supported_image_type(data: bytes) -> str | None:
    try:
        with Image.open(BytesIO(data)) as image:
            return ALLOWED_IMAGE_FORMATS.get((image.format or "").upper())
    except (UnidentifiedImageError, OSError, ValueError):
        return None


async def save_source_images(
    storage: Storage,
    files: list[UploadFile],
    session_id: str | None = None,
) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="at least one image is required")
    if len(files) > MAX_SOURCE_IMAGES:
        raise HTTPException(status_code=400, detail=f"maximum {MAX_SOURCE_IMAGES} images allowed")

    session = session_id or new_upload_session_id()
    asset_ids: list[str] = []
    for upload in files:
        content_type = (upload.content_type or "").split(";")[0].strip().lower()
        if content_type not in ALLOWED_IMAGE_MIMES:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported image type: {content_type or 'unknown'}",
            )
        data = await upload.read()
        if not data:
            raise HTTPException(status_code=400, detail="empty file is not allowed")
        if len(data) > MAX_SOURCE_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="image exceeds 10MB limit")
        detected_type = _detect_supported_image_type(data)
        if detected_type is None:
            raise HTTPException(
                status_code=400,
                detail="unsupported image content: upload JPEG or PNG",
            )
        if detected_type != content_type:
            raise HTTPException(
                status_code=400,
                detail=f"image content does not match declared type: {content_type}",
            )
        filename = upload.filename or "source.jpg"
        asset_id = storage.add_staging_asset(session, filename, data, detected_type)
        asset_ids.append(asset_id)

    return {"session_id": session, "asset_ids": asset_ids}
