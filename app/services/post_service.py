from datetime import datetime, timezone
import re

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.models.category import Category
from app.models.media import Media
from app.models.post import Post
from app.models.tag import Tag
from app.services.content_service import sanitize_html
from app.utils.slug import slugify


def unique_post_slug(db: Session, value: str, post_id: int | None = None) -> str:
    base = slugify(value)
    slug = base
    count = 2
    while True:
        query = select(Post).where(Post.slug == slug)
        if post_id is not None:
            query = query.where(Post.id != post_id)
        if db.scalar(query) is None:
            return slug
        slug = f"{base}-{count}"
        count += 1


def estimate_read_time(content: str | None) -> int:
    words = re.findall(r"\w+", content or "")
    return max(1, (len(words) + 199) // 200)


def format_views_count(count: int | None) -> str:
    value = count or 0
    if value == 1:
        return "1 view"
    for threshold, suffix in ((1_000_000_000, "b"), (1_000_000, "m"), (1_000, "k")):
        if value >= threshold:
            short = f"{value / threshold:.1f}".removesuffix(".0")
            return f"{short}{suffix} views"
    return f"{value} views"


def published_time_label(published_at: datetime | None, now: datetime | None = None) -> str:
    if published_at is None:
        return "Not published yet"
    current = now or datetime.now(timezone.utc)
    published = published_at if published_at.tzinfo else published_at.replace(tzinfo=timezone.utc)
    seconds = max(0, int((current - published).total_seconds()))
    if seconds < 60:
        return "Just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    if days <= 7:
        return f"{days} day{'s' if days != 1 else ''} ago"
    return published.strftime("%b %d, %Y")


def published_posts_query():
    return (
        select(Post)
        .where(Post.status == "published")
        .options(selectinload(Post.category), selectinload(Post.tags), selectinload(Post.author))
    )


def latest_posts(db: Session, limit: int = 9, exclude_post_id: int | None = None) -> list[Post]:
    query = published_posts_query()
    if exclude_post_id is not None:
        query = query.where(Post.id != exclude_post_id)
    return db.scalars(query.order_by(Post.published_at.desc().nullslast(), Post.created_at.desc()).limit(limit)).all()


def latest_posts_with_images(db: Session, limit: int = 9, exclude_post_id: int | None = None) -> list[Post]:
    query = published_posts_query().where(Post.featured_image_url != "")
    if exclude_post_id is not None:
        query = query.where(Post.id != exclude_post_id)
    return db.scalars(query.order_by(Post.published_at.desc().nullslast(), Post.created_at.desc()).limit(limit)).all()


def trending_posts(db: Session, limit: int = 5) -> list[Post]:
    return db.scalars(published_posts_query().order_by(Post.views_count.desc(), Post.published_at.desc().nullslast()).limit(limit)).all()


def trending_posts_with_images(db: Session, limit: int = 5, exclude_post_id: int | None = None) -> list[Post]:
    query = published_posts_query().where(Post.featured_image_url != "")
    if exclude_post_id is not None:
        query = query.where(Post.id != exclude_post_id)
    return db.scalars(query.order_by(Post.views_count.desc(), Post.published_at.desc().nullslast()).limit(limit)).all()


def featured_post(db: Session) -> Post | None:
    return db.scalar(published_posts_query().where(Post.is_featured.is_(True)).order_by(Post.published_at.desc().nullslast()).limit(1))


def homepage_featured_post(db: Session) -> Post | None:
    featured = db.scalar(
        published_posts_query()
        .where(Post.is_featured.is_(True), Post.featured_image_url != "")
        .order_by(Post.published_at.desc().nullslast())
        .limit(1)
    )
    if featured is not None:
        return featured
    return db.scalar(
        published_posts_query()
        .where(Post.featured_image_url != "")
        .order_by(Post.published_at.desc().nullslast(), Post.created_at.desc())
        .limit(1)
    )


def published_posts_count(db: Session) -> int:
    return db.scalar(select(func.count(Post.id)).where(Post.status == "published")) or 0


def paginated_posts(db: Session, offset: int, limit: int) -> list[Post]:
    return db.scalars(published_posts_query().order_by(Post.published_at.desc().nullslast(), Post.created_at.desc()).offset(offset).limit(limit)).all()


def get_published_post(db: Session, slug: str) -> Post | None:
    return db.scalar(published_posts_query().where(Post.slug == slug))


def increment_views(db: Session, post: Post) -> None:
    db.execute(update(Post).where(Post.id == post.id).values(views_count=Post.views_count + 1))
    db.commit()
    db.refresh(post)


def search_posts(db: Session, q: str) -> list[Post]:
    term = f"%{q.strip()}%"
    if not q.strip():
        return []
    return db.scalars(
        published_posts_query()
        .join(Category, Post.category_id == Category.id, isouter=True)
        .join(Post.tags, isouter=True)
        .where(
            or_(
                Post.title.ilike(term),
                Post.summary.ilike(term),
                Post.content.ilike(term),
                Category.name.ilike(term),
                Tag.name.ilike(term),
            )
        )
        .distinct()
        .order_by(Post.published_at.desc().nullslast(), Post.created_at.desc())
    ).all()


def posts_by_category(db: Session, category: Category) -> list[Post]:
    return db.scalars(published_posts_query().where(Post.category_id == category.id).order_by(Post.published_at.desc().nullslast())).all()


def posts_by_tag(db: Session, tag: Tag) -> list[Post]:
    return db.scalars(published_posts_query().join(Post.tags).where(Tag.id == tag.id).order_by(Post.published_at.desc().nullslast())).all()


def related_posts(db: Session, post: Post, limit: int = 6) -> list[Post]:
    related: list[Post] = []
    seen_ids = {post.id}

    if post.category:
        for item in posts_by_category(db, post.category):
            if item.id not in seen_ids:
                related.append(item)
                seen_ids.add(item.id)
            if len(related) >= limit:
                return related

    tag_ids = [tag.id for tag in post.tags]
    if tag_ids:
        tagged_posts = db.scalars(
            published_posts_query()
            .join(Post.tags)
            .where(Tag.id.in_(tag_ids), Post.id != post.id)
            .distinct()
            .order_by(Post.published_at.desc().nullslast(), Post.created_at.desc())
            .limit(limit * 2)
        ).all()
        for item in tagged_posts:
            if item.id not in seen_ids:
                related.append(item)
                seen_ids.add(item.id)
            if len(related) >= limit:
                break

    return related


def create_or_update_post(
    db: Session,
    *,
    title: str,
    slug: str,
    summary: str,
    content: str,
    status: str,
    is_featured: bool,
    category_id: int | None,
    tag_ids: list[int],
    author_id: int | None,
    featured_image_url: str = "",
    post: Post | None = None,
) -> Post:
    clean_status = "published" if status == "published" else "draft"
    final_slug = unique_post_slug(db, slug or title, post.id if post else None)
    if post is None:
        post = Post(title=title.strip(), slug=final_slug, content=sanitize_html(content), author_id=author_id)
        db.add(post)
    post.title = title.strip()
    post.slug = final_slug
    post.summary = summary.strip()
    post.content = sanitize_html(content)
    post.status = clean_status
    post.is_featured = is_featured
    post.category_id = category_id
    if featured_image_url:
        post.featured_image_url = featured_image_url
    if clean_status == "published" and post.published_at is None:
        post.published_at = datetime.now(timezone.utc)
    if clean_status == "draft":
        post.published_at = None
    post.tags = db.scalars(select(Tag).where(Tag.id.in_(tag_ids))).all() if tag_ids else []
    db.commit()
    db.refresh(post)
    return post


def dashboard_counts(db: Session) -> dict[str, int]:
    return {
        "total_posts": db.scalar(select(func.count(Post.id))) or 0,
        "published_posts": db.scalar(select(func.count(Post.id)).where(Post.status == "published")) or 0,
        "draft_posts": db.scalar(select(func.count(Post.id)).where(Post.status == "draft")) or 0,
        "categories": db.scalar(select(func.count(Category.id))) or 0,
        "tags": db.scalar(select(func.count(Tag.id))) or 0,
        "media": db.scalar(select(func.count(Media.id))) or 0,
    }
