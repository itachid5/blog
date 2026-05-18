from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.auth_service import authenticate_admin

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/admin/login")
def login_form(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/admin/dashboard", status_code=303)
    return templates.TemplateResponse("admin/login.html", {"request": request, "error": ""})


@router.post("/admin/login")
def login(request: Request, db: Session = Depends(get_db), username: str = Form(...), password: str = Form(...)):
    user = authenticate_admin(db, username.strip(), password)
    if user is None:
        return templates.TemplateResponse("admin/login.html", {"request": request, "error": "Invalid admin credentials."}, status_code=401)
    request.session["user_id"] = user.id
    return RedirectResponse("/admin/dashboard", status_code=303)


@router.get("/admin/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)
