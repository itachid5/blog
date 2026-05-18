import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import struct
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.database import SessionLocal, init_db
from app.models.category import Category
from app.models.post import Post
from app.models.tag import Tag
from app.models.user import User
from app.services.media_service import MediaUploadError, upload_featured_image
from app.utils.slug import slugify


@dataclass
class DemoUploadFile:
    filename: str
    content_type: str
    content: bytes

    async def read(self) -> bytes:
        return self.content


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def demo_png(width: int, height: int, start: tuple[int, int, int], end: tuple[int, int, int]) -> bytes:
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            mix = (x + y) / max(1, width + height - 2)
            row.extend(int(start[i] * (1 - mix) + end[i] * mix) for i in range(3))
        rows.append(b"\x00" + bytes(row))
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)) + png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + png_chunk(b"IEND", b"")


DEMO_CATEGORIES = [
    ("Engineering", "Architecture notes, implementation details, and practical software decisions."),
    ("Product", "Product thinking, launch notes, and user-centered tradeoffs."),
    ("Design", "Interface polish, content structure, and visual systems."),
    ("Operations", "Deployment, reliability, and maintenance lessons."),
]

DEMO_TAGS = ["FastAPI", "UX", "Python", "Launch", "Testing", "Cloud", "Frontend", "Data"]

DEMO_POSTS = [
    ("Designing a Faster Editorial Workflow", "A practical look at reducing friction in a small publishing stack.", "Product", ["UX", "Launch"], 184),
    ("FastAPI Patterns That Keep Routes Small", "How focused service functions make route handlers easier to test.", "Engineering", ["FastAPI", "Python"], 312),
    ("What Makes a Blog Homepage Feel Alive", "Balancing featured stories, recency, and discovery modules.", "Design", ["UX", "Frontend"], 241),
    ("Shipping Search Without Overbuilding It", "Simple search affordances that cover the most common reader needs.", "Product", ["Testing", "UX"], 96),
    ("The Case for Boring Deployment Checks", "Why health checks, logs, and repeatable commands matter more than cleverness.", "Operations", ["Cloud", "Testing"], 277),
    ("A Small Guide to Post Metadata", "Dates, read time, categories, tags, and views help readers choose what to open.", "Design", ["Frontend", "UX"], 153),
    ("Keeping Admin Forms Friendly", "File uploads and validation states should explain failures clearly.", "Engineering", ["FastAPI", "UX"], 221),
    ("How Tags Improve Content Discovery", "Tags work best when they express recurring themes instead of one-off labels.", "Product", ["Data", "UX"], 138),
    ("Testing Public Pages Like a Reader", "End-to-end checks catch navigation, image, and layout issues unit tests miss.", "Engineering", ["Testing", "Frontend"], 354),
    ("Planning a Lightweight Content Taxonomy", "A practical structure for categories, tags, and related post journeys.", "Product", ["Data", "Launch"], 119),
    ("Making Image Uploads Observable", "Useful logs can make external media failures much easier to diagnose.", "Operations", ["Cloud", "Testing"], 402),
    ("Responsive Cards That Carry Their Weight", "Post cards should communicate topic, timing, popularity, and next action.", "Design", ["Frontend", "UX"], 288),
    ("When to Refresh Data After a Write", "Refreshing ORM objects after counters change avoids stale UI details.", "Engineering", ["Python", "Testing"], 199),
    ("What Trending Lists Should Optimize For", "Popularity modules are most useful when they reveal genuine reader interest.", "Product", ["Data", "UX"], 467),
    ("A Calm Launch Checklist for Small Apps", "A compact checklist for routes, data, media, and browser verification.", "Operations", ["Launch", "Cloud"], 176),
]

PALETTE = [
    ((37, 99, 235), (147, 197, 253)),
    ((15, 23, 42), (45, 212, 191)),
    ((220, 38, 38), (254, 202, 202)),
    ((124, 58, 237), (221, 214, 254)),
    ((22, 163, 74), (187, 247, 208)),
]


async def upload_demo_images(count: int) -> list[str]:
    urls = []
    for index in range(count):
        start, end = PALETTE[index % len(PALETTE)]
        image = demo_png(960, 540, start, end)
        file = DemoUploadFile(filename=f"demo-blog-image-{index + 1}.png", content_type="image/png", content=image)
        urls.append(await upload_featured_image(file))
    return urls


def get_or_create_category(db, name: str, description: str) -> Category:
    category = db.scalar(select(Category).where(Category.name == name))
    if category is None:
        category = Category(name=name, slug=slugify(name), description=description)
        db.add(category)
        db.flush()
    elif not category.description:
        category.description = description
    return category


def get_or_create_tag(db, name: str) -> Tag:
    tag = db.scalar(select(Tag).where(Tag.name == name))
    if tag is None:
        tag = Tag(name=name, slug=slugify(name))
        db.add(tag)
        db.flush()
    return tag


def seed_posts(image_urls: list[str]) -> tuple[int, int, int]:
    init_db()
    db = SessionLocal()
    try:
        author = db.scalar(select(User).order_by(User.id))
        categories = {name: get_or_create_category(db, name, description) for name, description in DEMO_CATEGORIES}
        tags = {name: get_or_create_tag(db, name) for name in DEMO_TAGS}
        now = datetime.now(timezone.utc)
        created = 0
        updated = 0
        for index, (title, summary, category_name, tag_names, views_count) in enumerate(DEMO_POSTS):
            slug = slugify(title)
            post = db.scalar(select(Post).where(Post.slug == slug))
            content = (
                f"{summary}\n\n"
                "This demo article is seeded to exercise public browsing, taxonomy pages, search, related posts, image rendering, and view-count sorting.\n\n"
                "It uses the same Post, Category, Tag, and media upload flow as regular admin-created content."
            )
            if post is None:
                post = Post(title=title, slug=slug, content=content, author_id=author.id if author else None)
                db.add(post)
                created += 1
            else:
                updated += 1
            post.summary = summary
            post.content = content
            post.status = "published"
            post.is_featured = index == 0
            post.category = categories[category_name]
            post.tags = [tags[name] for name in tag_names]
            post.featured_image_url = image_urls[index]
            post.views_count = views_count
            post.published_at = now - timedelta(days=index)
        db.commit()
        return len(categories), len(tags), created + updated
    finally:
        db.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed demo blog posts with images uploaded through the configured media API.")
    parser.add_argument("--posts", type=int, default=len(DEMO_POSTS), choices=[len(DEMO_POSTS)], help="Number of demo posts to seed.")
    args = parser.parse_args()
    try:
        image_urls = await upload_demo_images(args.posts)
    except MediaUploadError as exc:
        raise SystemExit(f"Demo seed aborted: media upload failed before database writes. {exc}") from exc
    category_count, tag_count, post_count = seed_posts(image_urls)
    print(f"Seeded {category_count} categories, {tag_count} tags, {post_count} posts, and {len(image_urls)} uploaded images.")


if __name__ == "__main__":
    asyncio.run(main())
