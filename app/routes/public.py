from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.category import Category
from app.models.page import Page
from app.models.tag import Tag
from app.services import category_service, post_service, tag_service
from app.services.setting_service import get_settings_map
from app.utils.pagination import Pagination

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def public_context(db: Session) -> dict:
    return {
        "settings": get_settings_map(db),
        "categories": db.scalars(select(Category).order_by(Category.name)).all(),
        "tags": db.scalars(select(Tag).order_by(Tag.name)).all(),
        "menu_categories": category_service.menu_categories(db),
        "menu_tags": tag_service.menu_tags(db),
        "trending_posts": post_service.trending_posts(db),
        "estimate_read_time": post_service.estimate_read_time,
        "published_time_label": post_service.published_time_label,
        "format_views_count": post_service.format_views_count,
    }


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    context = public_context(db)
    featured = post_service.homepage_featured_post(db)
    selected_home_categories = category_service.menu_categories(db)
    selected_home_tags = tag_service.menu_tags(db)
    context.update(
        {
            "request": request,
            "latest_posts": post_service.latest_posts_with_images(db, 8, featured.id if featured else None),
            "featured_post": featured,
            "trending_posts": post_service.trending_posts_with_images(db, 5),
            "selected_home_categories": selected_home_categories,
            "selected_home_tags": selected_home_tags,
        }
    )
    return templates.TemplateResponse("home.html", context)


@router.get("/posts")
def all_posts(request: Request, page: int = Query(1, ge=1), db: Session = Depends(get_db)):
    pagination = Pagination(page=page, per_page=10, total=post_service.published_posts_count(db))
    context = public_context(db)
    context.update({"request": request, "posts": post_service.paginated_posts(db, pagination.offset, pagination.per_page), "pagination": pagination})
    return templates.TemplateResponse("posts.html", context)


@router.get("/categories")
def categories_index(request: Request, db: Session = Depends(get_db)):
    context = public_context(db)
    context.update({"request": request, "category_items": category_service.categories_with_published_counts(db)})
    return templates.TemplateResponse("categories.html", context)


@router.get("/tags")
def tags_index(request: Request, db: Session = Depends(get_db)):
    context = public_context(db)
    context.update({"request": request, "tag_items": tag_service.tags_with_published_counts(db)})
    return templates.TemplateResponse("tags.html", context)


@router.get("/post/{slug}")
def post_detail(slug: str, request: Request, db: Session = Depends(get_db)):
    post = post_service.get_published_post(db, slug)
    if post is None:
        raise HTTPException(status_code=404)
    post_service.increment_views(db, post)
    context = public_context(db)
    context.update({"request": request, "post": post, "related_posts": post_service.related_posts(db, post)})
    return templates.TemplateResponse("post_detail.html", context)


@router.get("/category/{slug}")
def category_page(slug: str, request: Request, db: Session = Depends(get_db)):
    category = db.scalar(select(Category).where(Category.slug == slug))
    if category is None:
        raise HTTPException(status_code=404)
    context = public_context(db)
    context.update({"request": request, "category": category, "posts": post_service.posts_by_category(db, category)})
    return templates.TemplateResponse("category.html", context)


@router.get("/tag/{slug}")
def tag_page(slug: str, request: Request, db: Session = Depends(get_db)):
    tag = db.scalar(select(Tag).where(Tag.slug == slug))
    if tag is None:
        raise HTTPException(status_code=404)
    context = public_context(db)
    context.update({"request": request, "tag": tag, "posts": post_service.posts_by_tag(db, tag)})
    return templates.TemplateResponse("tag.html", context)


@router.get("/search")
def search(request: Request, q: str = Query(""), db: Session = Depends(get_db)):
    context = public_context(db)
    context.update({"request": request, "q": q, "results": post_service.search_posts(db, q)})
    return templates.TemplateResponse("search.html", context)


def render_static_page(slug: str, request: Request, db: Session):
    page = db.scalar(select(Page).where(Page.slug == slug, Page.status == "published"))
    context = public_context(db)
    context.update({"request": request, "page": page, "title": page.title if page else slug.title(), "content": page.content if page else "This page is being prepared."})
    return templates.TemplateResponse(f"{slug}.html", context)


@router.get("/about")
def about(request: Request, db: Session = Depends(get_db)):
    return render_static_page("about", request, db)


@router.get("/contact")
def contact(request: Request, db: Session = Depends(get_db)):
    return render_static_page("contact", request, db)


@router.get("/privacy")
def privacy(request: Request, db: Session = Depends(get_db)):
    return render_static_page("privacy", request, db)


@router.get("/terms")
def terms(request: Request, db: Session = Depends(get_db)):
    return render_static_page("terms", request, db)
