import logging
from pathlib import Path

from fastapi import UploadFile
import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.media import Media

logger = logging.getLogger(__name__)


class MediaUploadError(Exception):
    pass


def _payload_data(payload: dict) -> dict:
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def normalize_media_payload(payload: dict, filename: str) -> dict:
    data = _payload_data(payload)
    secure_url = payload.get("secure_url") or data.get("secure_url") or payload.get("url") or data.get("url")
    if not secure_url:
        logger.warning("Media upload response missing secure_url: %s", payload)
        raise MediaUploadError("Media upload service returned no media URL.")
    resource_type = payload.get("resource_type") or data.get("resource_type") or "image"
    format_value = payload.get("format") or data.get("format") or Path(filename).suffix.removeprefix(".")
    return {
        "original_filename": filename,
        "secure_url": secure_url,
        "public_id": payload.get("public_id") or data.get("public_id") or "",
        "resource_type": resource_type,
        "format": format_value or "",
        "bytes": int(payload.get("bytes") or data.get("bytes") or 0),
        "width": payload.get("width") or data.get("width"),
        "height": payload.get("height") or data.get("height"),
    }


async def upload_media(file: UploadFile | None) -> dict:
    if file is None or not file.filename:
        raise MediaUploadError("Choose a file to upload.")
    data = await file.read()
    if not data:
        raise MediaUploadError("Choose a non-empty file to upload.")
    files = {"file": (file.filename, data, file.content_type or "application/octet-stream")}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(get_settings().media_upload_url, files=files)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("Media upload failed with status %s: %s", exc.response.status_code, exc.response.text[:500])
        raise MediaUploadError("Media upload service is unavailable. Please try again later.") from exc
    except httpx.HTTPError as exc:
        logger.warning("Media upload request failed: %s", exc)
        raise MediaUploadError("Media upload service is unavailable. Please try again later.") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("Media upload returned invalid JSON: %s", response.text[:500])
        raise MediaUploadError("Media upload service returned an invalid response.") from exc
    return normalize_media_payload(payload, file.filename)


async def upload_featured_image(file: UploadFile | None) -> str:
    if file is None or not file.filename:
        return ""
    return (await upload_media(file))["secure_url"]


async def upload_and_save_media(db: Session, file: UploadFile | None, uploaded_by: int | None = None) -> Media:
    metadata = await upload_media(file)
    media = Media(**metadata, uploaded_by=uploaded_by, title=metadata["original_filename"])
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def format_file_size(value: int | None) -> str:
    size = value or 0
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" and size % 1 else f"{int(size)} {unit}"
        size /= 1024
    return "0 B"
