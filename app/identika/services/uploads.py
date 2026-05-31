from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException, UploadFile

if TYPE_CHECKING:
    from identika.storage import Storage

MAX_SOURCE_IMAGES = 4
MAX_SOURCE_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def new_upload_session_id() -> str:
    return uuid.uuid4().hex


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
        filename = upload.filename or "source.jpg"
        asset_id = storage.add_staging_asset(session, filename, data, content_type)
        asset_ids.append(asset_id)

    return {"session_id": session, "asset_ids": asset_ids}
