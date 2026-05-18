from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.tag import Tag
from app.routes.deps import require_admin
from app.services.setting_service import get_settings_map
from app.services.tag_service import create_tag, update_tag

router = APIRouter(prefix="/admin/tags")
templates = Jinja2Templates(directory="app/templates")


@router.get("")
def tags(request: Request, db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    items = db.scalars(select(Tag).order_by(Tag.name)).all()
    return templates.TemplateResponse("admin/tags.html", {"request": request, "admin": admin, "tags": items, "settings": get_settings_map(db)})


@router.post("")
def create(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    show_in_menu: bool = Form(False),
    menu_order: int = Form(0),
):
    require_admin(request, db)
    if name.strip():
        create_tag(db, name, show_in_menu, menu_order)
    return RedirectResponse("/admin/tags", status_code=303)


@router.post("/{tag_id}/update")
def update(
    tag_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    show_in_menu: bool = Form(False),
    menu_order: int = Form(0),
):
    require_admin(request, db)
    tag = db.get(Tag, tag_id)
    if tag and name.strip():
        update_tag(db, tag, name, show_in_menu, menu_order)
    return RedirectResponse("/admin/tags", status_code=303)


@router.post("/{tag_id}/delete")
def delete(tag_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tag = db.get(Tag, tag_id)
    if tag:
        db.delete(tag)
        db.commit()
    return RedirectResponse("/admin/tags", status_code=303)
