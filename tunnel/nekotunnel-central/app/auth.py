from fastapi import Request
from starlette.responses import RedirectResponse

from .config import settings


SESSION_KEY = "admin_authenticated"


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def require_admin(request: Request):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return None


def login_admin(request: Request) -> None:
    request.session[SESSION_KEY] = True


def logout_admin(request: Request) -> None:
    request.session.clear()
