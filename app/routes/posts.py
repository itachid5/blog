from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.models.category import Category
from app.models.media import Media
from app.models.post import Post
from app.models.tag import Tag
from app.routes.deps import require_admin
from app.services.media_service import MediaUploadError, upload_and_save_media
from app.services.post_service import create_or_update_post, format_views_count
from app.services.setting_service import get_settings_map

router = APIRouter(prefix="/admin/posts")
templates = Jinja2Templates(directory="app/templates")


async def featured_image_url_from_form(db: Session, featured_image: UploadFile | None, featured_image_url: str, admin_id: int | None, existing_url: str = "") -> str:
    if featured_image is not None and featured_image.filename:
        return (await upload_and_save_media(db, featured_image, uploaded_by=admin_id)).secure_url
    if featured_image_url.strip():
        return featured_image_url.strip()
    return existing_url


def form_data(db: Session) -> dict:
    return {
        "categories": db.scalars(select(Category).order_by(Category.name)).all(),
        "tags": db.scalars(select(Tag).order_by(Tag.name)).all(),
        "media_items": db.scalars(select(Media).where(Media.resource_type == "image").order_by(Media.created_at.desc()).limit(24)).all(),
        "settings": get_settings_map(db),
    }


@router.get("")
def posts_list(request: Request, db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    posts = db.scalars(select(Post).options(selectinload(Post.category), selectinload(Post.tags)).order_by(Post.created_at.desc())).all()
    return templates.TemplateResponse("admin/posts_list.html", {"request": request, "admin": admin, "posts": posts, "settings": get_settings_map(db), "format_views_count": format_views_count})


@router.get("/create")
def create_form(request: Request, db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    context = {"request": request, "admin": admin, "post": None, "error": ""}
    context.update(form_data(db))
    return templates.TemplateResponse("admin/post_create.html", context)


@router.post("/create")
async def create_post(
    request: Request,
    db: Session = Depends(get_db),
    title: str = Form(...),
    slug: str = Form(""),
    summary: str = Form(""),
    content: str = Form(...),
    status: str = Form("draft"),
    category_id: int | None = Form(None),
    tag_ids: list[int] = Form([]),
    is_featured: bool = Form(False),
    featured_image: UploadFile | None = File(None),
    featured_image_url: str = Form(""),
):
    admin = require_admin(request, db)
    try:
        image_url = await featured_image_url_from_form(db, featured_image, featured_image_url, admin.id)
        if not image_url:
            context = {"request": request, "admin": admin, "post": None, "error": "Featured image is required."}
            context.update(form_data(db))
            return templates.TemplateResponse("admin/post_create.html", context, status_code=400)
        create_or_update_post(db, title=title, slug=slug, summary=summary, content=content, status=status, is_featured=is_featured, category_id=category_id, tag_ids=tag_ids, author_id=admin.id, featured_image_url=image_url)
    except MediaUploadError as exc:
        context = {"request": request, "admin": admin, "post": None, "error": str(exc)}
        context.update(form_data(db))
        return templates.TemplateResponse("admin/post_create.html", context, status_code=400)
    return RedirectResponse("/admin/posts", status_code=303)


@router.get("/{post_id}/edit")
def edit_form(post_id: int, request: Request, db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    post = db.scalar(select(Post).options(selectinload(Post.tags)).where(Post.id == post_id))
    if post is None:
        return RedirectResponse("/admin/posts", status_code=303)
    context = {"request": request, "admin": admin, "post": post, "error": ""}
    context.update(form_data(db))
    return templates.TemplateResponse("admin/post_edit.html", context)


@router.post("/{post_id}/edit")
async def update_post(
    post_id: int,
    request: Request,
    db: Session = Depends(get_db),
    title: str = Form(...),
    slug: str = Form(""),
    summary: str = Form(""),
    content: str = Form(...),
    status: str = Form("draft"),
    category_id: int | None = Form(None),
    tag_ids: list[int] = Form([]),
    is_featured: bool = Form(False),
    featured_image: UploadFile | None = File(None),
    featured_image_url: str = Form(""),
):
    admin = require_admin(request, db)
    post = db.scalar(select(Post).options(selectinload(Post.tags)).where(Post.id == post_id))
    if post is None:
        return RedirectResponse("/admin/posts", status_code=303)
    try:
        image_url = await featured_image_url_from_form(db, featured_image, featured_image_url, admin.id, post.featured_image_url)
        if not image_url:
            context = {"request": request, "admin": admin, "post": post, "error": "Featured image is required."}
            context.update(form_data(db))
            return templates.TemplateResponse("admin/post_edit.html", context, status_code=400)
        create_or_update_post(db, title=title, slug=slug, summary=summary, content=content, status=status, is_featured=is_featured, category_id=category_id, tag_ids=tag_ids, author_id=admin.id, featured_image_url=image_url, post=post)
    except MediaUploadError as exc:
        context = {"request": request, "admin": admin, "post": post, "error": str(exc)}
        context.update(form_data(db))
        return templates.TemplateResponse("admin/post_edit.html", context, status_code=400)
    return RedirectResponse("/admin/posts", status_code=303)


@router.post("/{post_id}/delete")
def delete_post(post_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    post = db.get(Post, post_id)
    if post:
        db.delete(post)
        db.commit()
    return RedirectResponse("/admin/posts", status_code=303)
