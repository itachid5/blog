from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.routes.deps import require_admin
from app.services.setting_service import get_settings_map, upsert_setting

router = APIRouter(prefix="/admin/settings")
templates = Jinja2Templates(directory="app/templates")


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    return templates.TemplateResponse("admin/settings.html", {"request": request, "admin": admin, "settings": get_settings_map(db)})


@router.post("")
def update_settings(
    request: Request,
    db: Session = Depends(get_db),
    site_name: str = Form(...),
    site_description: str = Form(""),
    logo_url: str = Form(""),
    twitter_url: str = Form(""),
    github_url: str = Form(""),
    linkedin_url: str = Form(""),
):
    require_admin(request, db)
    for key, value in {
        "site_name": site_name,
        "site_description": site_description,
        "logo_url": logo_url,
        "twitter_url": twitter_url,
        "github_url": github_url,
        "linkedin_url": linkedin_url,
    }.items():
        upsert_setting(db, key, value.strip())
    db.commit()
    return RedirectResponse("/admin/settings", status_code=303)
