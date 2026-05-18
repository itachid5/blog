from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.media import Media
from app.routes.deps import require_admin
from app.services.media_service import MediaUploadError, format_file_size, upload_and_save_media
from app.services.setting_service import get_settings_map

router = APIRouter(prefix="/admin/media")
templates = Jinja2Templates(directory="app/templates")


@router.get("")
def media_library(request: Request, q: str = "", resource_type: str = "", db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    query = select(Media).order_by(Media.created_at.desc())
    clean_type = resource_type.strip()
    clean_q = q.strip()
    if clean_type:
        query = query.where(Media.resource_type == clean_type)
    if clean_q:
        term = f"%{clean_q}%"
        query = query.where(or_(Media.original_filename.ilike(term), Media.title.ilike(term), Media.alt_text.ilike(term), Media.description.ilike(term), Media.secure_url.ilike(term)))
    media_items = db.scalars(query).all()
    return templates.TemplateResponse(
        "admin/media.html",
        {
            "request": request,
            "admin": admin,
            "settings": get_settings_map(db),
            "media_items": media_items,
            "q": clean_q,
            "resource_type": clean_type,
            "error": request.query_params.get("error", ""),
            "format_file_size": format_file_size,
        },
    )


@router.post("/upload")
async def upload_media_item(request: Request, db: Session = Depends(get_db), file: UploadFile | None = File(None)):
    admin = require_admin(request, db)
    try:
        await upload_and_save_media(db, file, uploaded_by=admin.id)
    except MediaUploadError as exc:
        return RedirectResponse(f"/admin/media?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/admin/media", status_code=303)


@router.post("/{media_id}/delete")
def remove_media_item(media_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    media = db.get(Media, media_id)
    if media:
        db.delete(media)
        db.commit()
    return RedirectResponse("/admin/media", status_code=303)
