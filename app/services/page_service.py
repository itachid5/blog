from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.page import Page
from app.utils.slug import slugify


def unique_page_slug(db: Session, value: str, page_id: int | None = None) -> str:
    base = slugify(value)
    slug = base
    count = 2
    while True:
        query = select(Page).where(Page.slug == slug)
        if page_id is not None:
            query = query.where(Page.id != page_id)
        if db.scalar(query) is None:
            return slug
        slug = f"{base}-{count}"
        count += 1
