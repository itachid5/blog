from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import get_settings
from app.core.database import init_db, seed_default_data
from app.routes import admin, auth, categories, media, pages, posts, public, settings as settings_routes, tags

settings = get_settings()
templates = Jinja2Templates(directory="app/templates")


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="blog_admin_session",
        same_site="lax",
        https_only=settings.is_production,
        max_age=60 * 60 * 12,
    )
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.include_router(public.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(posts.router)
    app.include_router(categories.router)
    app.include_router(tags.router)
    app.include_router(pages.router)
    app.include_router(media.router)
    app.include_router(settings_routes.router)

    @app.on_event("startup")
    def startup() -> None:
        init_db()
        seed_default_data()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception) -> HTMLResponse:
        return templates.TemplateResponse("base.html", {"request": request, "settings": {"site_name": settings.app_name}, "not_found": True}, status_code=404)

    return app


app = create_app()
