from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.page import Page
from app.routes.deps import require_admin
from app.services.content_service import sanitize_html
from app.services.page_service import unique_page_slug
from app.services.setting_service import get_settings_map

router = APIRouter(prefix="/admin/pages")
templates = Jinja2Templates(directory="app/templates")


@router.get("")
def pages(request: Request, db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    return templates.TemplateResponse("admin/pages.html", {"request": request, "admin": admin, "pages": db.scalars(select(Page).order_by(Page.title)).all(), "settings": get_settings_map(db)})


@router.post("")
def save_page(request: Request, db: Session = Depends(get_db), page_id: int | None = Form(None), title: str = Form(...), slug: str = Form(""), content: str = Form(...), status: str = Form("draft")):
    require_admin(request, db)
    page = db.get(Page, page_id) if page_id else None
    if page is None:
        page = Page(title=title.strip(), slug=unique_page_slug(db, slug or title), content=sanitize_html(content), status=status)
        db.add(page)
    else:
        page.title = title.strip()
        page.slug = unique_page_slug(db, slug or title, page.id)
        page.content = sanitize_html(content)
        page.status = "published" if status == "published" else "draft"
    db.commit()
    return RedirectResponse("/admin/pages", status_code=303)


@router.post("/{page_id}/delete")
def delete_page(page_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    page = db.get(Page, page_id)
    if page:
        db.delete(page)
        db.commit()
    return RedirectResponse("/admin/pages", status_code=303)
