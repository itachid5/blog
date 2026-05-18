from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.category import Category
from app.routes.deps import require_admin
from app.services.category_service import create_category, update_category
from app.services.setting_service import get_settings_map

router = APIRouter(prefix="/admin/categories")
templates = Jinja2Templates(directory="app/templates")


@router.get("")
def categories(request: Request, db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    items = db.scalars(select(Category).order_by(Category.name)).all()
    return templates.TemplateResponse("admin/categories.html", {"request": request, "admin": admin, "categories": items, "settings": get_settings_map(db), "error": ""})


@router.post("")
def create(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    description: str = Form(""),
    show_in_menu: bool = Form(False),
    menu_order: int = Form(0),
):
    require_admin(request, db)
    if name.strip():
        create_category(db, name, description, show_in_menu, menu_order)
    return RedirectResponse("/admin/categories", status_code=303)


@router.post("/{category_id}/update")
def update(
    category_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    description: str = Form(""),
    show_in_menu: bool = Form(False),
    menu_order: int = Form(0),
):
    require_admin(request, db)
    category = db.get(Category, category_id)
    if category and name.strip():
        update_category(db, category, name, description, show_in_menu, menu_order)
    return RedirectResponse("/admin/categories", status_code=303)


@router.post("/{category_id}/delete")
def delete(category_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    category = db.get(Category, category_id)
    if category:
        db.delete(category)
        db.commit()
    return RedirectResponse("/admin/categories", status_code=303)
