import re
from uuid import uuid4

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.media import Media
from app.models.page import Page
from app.models.post import Post
from app.services.media_service import MediaUploadError


def test_admin_pages_render(admin_client):
    for path in ["/admin/dashboard", "/admin/posts", "/admin/posts/create", "/admin/categories", "/admin/tags", "/admin/pages", "/admin/media", "/admin/settings"]:
        response = admin_client.get(path)
        assert response.status_code == 200
        assert "admin" in response.text.lower() or "dashboard" in response.text.lower()


def test_taxonomy_menu_controls(admin_client):
    suffix = uuid4().hex[:8]
    category_name = f"Menu Category {suffix}"
    tag_name = f"Menu Tag {suffix}"
    response = admin_client.post(
        "/admin/categories",
        data={"name": category_name, "description": "Shown", "show_in_menu": "true", "menu_order": "7"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = admin_client.post(
        "/admin/tags",
        data={"name": tag_name, "show_in_menu": "true", "menu_order": "3"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    categories = admin_client.get("/admin/categories")
    tags = admin_client.get("/admin/tags")
    assert category_name in categories.text
    assert tag_name in tags.text
    assert "value=\"7\"" in categories.text
    assert "value=\"3\"" in tags.text
    assert "checked" in categories.text
    assert "checked" in tags.text

    category_id = re.search(rf'value="{category_name}".*?/admin/categories/(\d+)/update', categories.text, re.S).group(1)
    tag_id = re.search(rf'value="{tag_name}".*?/admin/tags/(\d+)/update', tags.text, re.S).group(1)
    assert admin_client.post(f"/admin/categories/{category_id}/update", data={"name": category_name, "description": "Hidden", "menu_order": "9"}, follow_redirects=False).status_code == 303
    assert admin_client.post(f"/admin/tags/{tag_id}/update", data={"name": tag_name, "menu_order": "5"}, follow_redirects=False).status_code == 303
    assert "value=\"9\"" in admin_client.get("/admin/categories").text
    assert "value=\"5\"" in admin_client.get("/admin/tags").text


def test_settings_update(admin_client):
    response = admin_client.post(
        "/admin/settings",
        data={
            "site_name": "Blog Website",
            "site_description": "Updated from tests",
            "logo_url": "",
            "twitter_url": "",
            "github_url": "",
            "linkedin_url": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    home = admin_client.get("/")
    assert "Updated from tests" in home.text


def test_post_image_upload_failure_rerenders_form(admin_client, monkeypatch):
    async def fail_upload(db, file, uploaded_by=None):
        raise MediaUploadError("Media upload service returned no image URL.")

    monkeypatch.setattr("app.routes.posts.upload_and_save_media", fail_upload)
    response = admin_client.post(
        "/admin/posts/create",
        data={
            "title": "Upload Failure Test",
            "slug": "upload-failure-test",
            "summary": "Upload failure summary.",
            "content": "Upload failure content.",
            "status": "published",
        },
        files={"featured_image": ("failure.png", b"not really an image", "image/png")},
    )
    assert response.status_code == 400
    assert "Media upload service returned no image URL." in response.text
    assert "Save Post" in response.text


def test_media_upload_record_search_and_dashboard_counts(admin_client, monkeypatch):
    suffix = uuid4().hex[:8]
    media_url = f"https://cdn.example.test/media-{suffix}.png"
    filename = f"pytest-media-{suffix}.png"

    async def save_media(db, file, uploaded_by=None):
        media = Media(
            original_filename=file.filename,
            secure_url=media_url,
            public_id=f"pytest/{suffix}",
            resource_type="image",
            format="png",
            bytes=2048,
            width=800,
            height=600,
            uploaded_by=uploaded_by,
            title=file.filename,
        )
        db.add(media)
        db.commit()
        db.refresh(media)
        return media

    monkeypatch.setattr("app.routes.media.upload_and_save_media", save_media)
    response = admin_client.post(
        "/admin/media/upload",
        files={"file": (filename, b"fake image bytes", "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/media"

    library = admin_client.get(f"/admin/media?q={suffix}")
    assert library.status_code == 200
    assert filename in library.text
    assert media_url in library.text
    assert "Copy URL" in library.text
    assert "Remove from library" in library.text

    dashboard = admin_client.get("/admin/dashboard")
    assert dashboard.status_code == 200
    assert "Media Files" in dashboard.text


def test_post_requires_featured_image_and_accepts_media_url(admin_client):
    suffix = uuid4().hex[:8]
    title = f"Media URL Post {suffix}"
    slug = f"media-url-post-{suffix}"
    media_url = f"https://cdn.example.test/{slug}.jpg"

    missing_image = admin_client.post(
        "/admin/posts/create",
        data={
            "title": title,
            "slug": slug,
            "summary": "Missing image summary.",
            "content": "Missing image content.",
            "status": "published",
        },
    )
    assert missing_image.status_code == 400
    assert "Featured image is required." in missing_image.text

    response = admin_client.post(
        "/admin/posts/create",
        data={
            "title": title,
            "slug": slug,
            "summary": "Media image summary.",
            "content": "<h2>Allowed Heading</h2><script>alert('x')</script>",
            "status": "published",
            "featured_image_url": media_url,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    detail = admin_client.get(f"/post/{slug}")
    assert detail.status_code == 200
    assert media_url in detail.text
    assert "Allowed Heading" in detail.text
    assert "<script>alert" not in detail.text

    db = SessionLocal()
    try:
        post = db.scalar(select(Post).where(Post.slug == slug))
        assert post is not None
        assert post.featured_image_url == media_url
        assert "<script" not in post.content
    finally:
        db.close()


def test_page_editor_saves_sanitized_html(admin_client):
    suffix = uuid4().hex[:8]
    slug = f"sanitized-page-{suffix}"
    response = admin_client.post(
        "/admin/pages",
        data={
            "title": f"Sanitized Page {suffix}",
            "slug": slug,
            "content": "<h2>Clean Page</h2><script>alert('x')</script>",
            "status": "published",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    db = SessionLocal()
    try:
        page = db.scalar(select(Page).where(Page.slug == slug))
        assert page is not None
        assert "Clean Page" in page.content
        assert "<script" not in page.content
    finally:
        db.close()
