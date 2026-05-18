from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, init_db, seed_default_data
from app.models.category import Category
from app.models.post import Post
from app.models.tag import Tag
from app.models.user import User
from app.utils.slug import slugify

IMAGE_DIR = Path("tmp/demo_images")
POST_COUNT = 15
IMAGE_WIDTH = 1200
IMAGE_HEIGHT = 800
UPLOAD_URL = "https://bot-api-j75j.onrender.com/api/cloudinary/upload"


@dataclass
class SeedStats:
    downloaded: int = 0
    generated: int = 0
    uploaded: int = 0
    posts_with_featured_images: int = 0


DEMO_CATEGORIES = [
    ("Development", "development", "Practical engineering tutorials and platform notes."),
    ("Design", "design", "Interface design, content strategy, and user experience ideas."),
    ("Operations", "operations", "Deployment, monitoring, automation, and reliability guides."),
]

DEMO_TAGS = [
    ("FastAPI", "fastapi"),
    ("Python", "python"),
    ("Cloud", "cloud"),
    ("Frontend", "frontend"),
    ("Testing", "testing"),
    ("Automation", "automation"),
]

DEMO_POSTS = [
    "Building a FastAPI Blog That Feels Fast",
    "Designing Blog Cards Readers Actually Click",
    "Practical Deployment Checklist for Small Web Apps",
    "How to Structure Content for Search and Discovery",
    "A Simple Guide to Admin Workflows",
    "Using Categories Without Creating Clutter",
    "Tagging Strategies for Technical Blogs",
    "Improving Template Performance With Jinja",
    "Writing Better Post Summaries",
    "Polishing Empty States and Error Pages",
    "Testing Public Pages With Realistic Data",
    "Creating a Content Calendar That Ships",
    "Making Featured Posts Stand Out",
    "Search UX Patterns for Editorial Sites",
    "Keeping a Blog Maintainable Over Time",
]


def ensure_category(db: Session, name: str, slug: str, description: str) -> Category:
    category = db.scalar(select(Category).where(Category.slug == slug))
    if category is None:
        category = Category(name=name, slug=slug, description=description)
        db.add(category)
        db.commit()
        db.refresh(category)
    return category


def ensure_tag(db: Session, name: str, slug: str) -> Tag:
    tag = db.scalar(select(Tag).where(Tag.slug == slug))
    if tag is None:
        tag = Tag(name=name, slug=slug)
        db.add(tag)
        db.commit()
        db.refresh(tag)
    return tag


def demo_content(title: str, index: int) -> str:
    return "\n\n".join(
        [
            f"# {title}",
            "A strong blog experience starts with useful content, clear navigation, and visual hierarchy that helps readers decide what to open next.",
            f"This demo article number {index} includes realistic paragraph length so list pages, search results, category pages, tag pages, related posts, and the detail page all have meaningful content to render.",
            "Use the admin panel to replace this seeded copy with real posts when production content is ready.",
        ]
    )


def download_image(index: int, image_path: Path, stats: SeedStats) -> None:
    image_url = f"https://picsum.photos/seed/blog-post-{index}/1200/800"
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            response = client.get(image_url)
            response.raise_for_status()
        image_path.write_bytes(response.content)
        stats.downloaded += 1
        print(f"downloaded post-{index}: {image_url} -> {image_path}")
    except Exception as exc:
        print(f"download failed for post-{index}: {exc}; generating local placeholder")
        generate_placeholder_image(index, image_path)
        stats.generated += 1
        print(f"generated post-{index}: {image_path}")


def generate_placeholder_image(index: int, image_path: Path) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont

        colors = [
            (35, 78, 112),
            (87, 64, 115),
            (28, 107, 91),
            (143, 76, 43),
            (118, 47, 73),
        ]
        image = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), colors[(index - 1) % len(colors)])
        draw = ImageDraw.Draw(image)
        title = f"Blog Post {index}"
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 82)
            small_font = ImageFont.truetype("DejaVuSans.ttf", 36)
        except Exception:
            font = ImageFont.load_default()
            small_font = ImageFont.load_default()
        draw.text((90, 310), title, fill=(255, 255, 255), font=font)
        draw.text((94, 420), "Unique demo featured image", fill=(230, 238, 246), font=small_font)
        image.save(image_path, format="JPEG", quality=92)
    except ImportError:
        image_path.with_suffix(".png").write_bytes(generate_png_bytes(index))
        image_path.unlink(missing_ok=True)
        image_path = image_path.with_suffix(".png")


def generate_png_bytes(index: int) -> bytes:
    palette = [
        (35, 78, 112),
        (87, 64, 115),
        (28, 107, 91),
        (143, 76, 43),
        (118, 47, 73),
    ]
    base = palette[(index - 1) % len(palette)]
    rows = []
    for y in range(IMAGE_HEIGHT):
        row = bytearray([0])
        for x in range(IMAGE_WIDTH):
            wave = int(28 * math.sin((x + index * 37) / 65) + 28 * math.cos((y + index * 53) / 75))
            row.extend(
                (
                    max(0, min(255, base[0] + wave)),
                    max(0, min(255, base[1] + wave)),
                    max(0, min(255, base[2] + wave)),
                )
            )
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", IMAGE_WIDTH, IMAGE_HEIGHT, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def upload_image(image_path: Path, index: int, stats: SeedStats) -> str:
    content_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    with image_path.open("rb") as image_file:
        files = {"file": (image_path.name, image_file, content_type)}
        with httpx.Client(timeout=60) as client:
            response = client.post(UPLOAD_URL, files=files)
            response.raise_for_status()
    upload_data = response.json()
    secure_url = upload_data.get("secure_url") or upload_data.get("data", {}).get("secure_url")
    if not secure_url:
        raise RuntimeError(f"upload for post-{index} returned an empty secure_url: {upload_data}")
    stats.uploaded += 1
    print(f"uploaded post-{index}: {secure_url}")
    return secure_url


def prepare_uploaded_image(index: int, stats: SeedStats) -> str:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    image_path = IMAGE_DIR / f"post-{index}.jpg"
    download_image(index, image_path, stats)
    if not image_path.exists():
        image_path = image_path.with_suffix(".png")
    return upload_image(image_path, index, stats)


def upsert_demo_posts(db: Session, image_urls: list[str]) -> None:
    admin = db.scalar(select(User).order_by(User.id))
    categories = [ensure_category(db, *item) for item in DEMO_CATEGORIES]
    tags = [ensure_tag(db, *item) for item in DEMO_TAGS]
    now = datetime.now(timezone.utc)

    for index, title in enumerate(DEMO_POSTS, start=1):
        slug = slugify(title)
        post = db.scalar(select(Post).where(Post.slug == slug))
        if post is None:
            post = Post(title=title, slug=slug, author_id=admin.id if admin else None)
            db.add(post)
        post.summary = f"Demo article {index} with a unique uploaded featured image for blog card testing."
        post.content = demo_content(title, index)
        post.featured_image_url = image_urls[index - 1]
        post.status = "published"
        post.is_featured = index == 1
        post.views_count = 250 - index * 9
        post.category = categories[(index - 1) % len(categories)]
        post.tags = [tags[(index - 1) % len(tags)], tags[index % len(tags)]]
        post.published_at = now - timedelta(days=index - 1)
    db.commit()


def ensure_all_published_posts_have_unique_cloudinary_images(db: Session, start_index: int, stats: SeedStats) -> None:
    posts = db.scalars(select(Post).where(Post.status == "published").order_by(Post.id)).all()
    seen_urls: set[str] = set()
    image_index = start_index
    for post in posts:
        url = (post.featured_image_url or "").strip()
        needs_image = not url or "cloudinary" not in url.lower() or url in seen_urls
        if needs_image:
            image_index += 1
            post.featured_image_url = prepare_uploaded_image(image_index, stats)
            print(f"updated published post image: {post.slug} -> {post.featured_image_url}")
            url = post.featured_image_url
        seen_urls.add(url)
    db.commit()


def validate_published_posts(db: Session, stats: SeedStats) -> None:
    posts = db.scalars(select(Post).where(Post.status == "published").order_by(Post.id)).all()
    empty = [post.slug for post in posts if not (post.featured_image_url or "").strip()]
    non_cloudinary = [post.slug for post in posts if "cloudinary" not in (post.featured_image_url or "").lower()]
    urls = [post.featured_image_url for post in posts]
    duplicates = sorted({url for url in urls if urls.count(url) > 1})

    if empty:
        raise RuntimeError(f"published posts missing featured_image_url: {empty}")
    if non_cloudinary:
        raise RuntimeError(f"published posts without Cloudinary image URLs: {non_cloudinary}")
    if duplicates:
        raise RuntimeError(f"duplicate featured_image_url values found: {duplicates}")

    stats.posts_with_featured_images = len(posts)
    print(f"validated published posts with featured images: {len(posts)}")
    print("validated image URLs are unique and Cloudinary-hosted")


def main() -> None:
    init_db()
    seed_default_data()
    stats = SeedStats()
    image_urls = [prepare_uploaded_image(index, stats) for index in range(1, POST_COUNT + 1)]

    db = SessionLocal()
    try:
        upsert_demo_posts(db, image_urls)
        ensure_all_published_posts_have_unique_cloudinary_images(db, POST_COUNT, stats)
        validate_published_posts(db, stats)
    finally:
        db.close()

    print("seed complete")
    print(f"images downloaded: {stats.downloaded}")
    print(f"images generated: {stats.generated}")
    print(f"images uploaded: {stats.uploaded}")
    print(f"posts with featured images: {stats.posts_with_featured_images}")


if __name__ == "__main__":
    main()
