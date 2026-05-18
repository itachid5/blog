# Blog Website

A complete Python FastAPI blog website with a public frontend, custom admin panel, PostgreSQL database, and external media API upload integration.

## Stack

- Python
- FastAPI
- Uvicorn
- Jinja2 templates
- SQLAlchemy
- PostgreSQL
- Pydantic Settings / python-dotenv
- passlib + bcrypt
- httpx
- pytest
- Playwright MCP/browser for manual UI verification

## Environment

Create `.env` from `.env.example` and fill in real values.

Required values:

```env
PORT=6000
APP_NAME=Blog Website
APP_ENV=development
SECRET_KEY=change-this-secret-key
DATABASE_URL=postgresql://user:password@host/database?sslmode=require
MEDIA_API_BASE_URL=http://127.0.0.1:8000
MEDIA_UPLOAD_ENDPOINT=/api/cloudinary/upload
ADMIN_USERNAME=admin
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=change-this-password
```

This project is configured to use PostgreSQL only.

## Install

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run

The app creates missing tables and seeds the default admin/settings/pages on startup.

```bash
uvicorn app.main:app --host 127.0.0.1 --port 6000
```

Local URL:

```text
http://127.0.0.1:6000
```

## Admin Login

Admin credentials come from `.env`:

- Username: `ADMIN_USERNAME`
- Email: `ADMIN_EMAIL`
- Password: `ADMIN_PASSWORD`

Default local values:

- Username: `admin`
- Email: `admin@example.com`
- Password: `change-this-password`

## Public Pages

- `/`
- `/post/{slug}`
- `/category/{slug}`
- `/tag/{slug}`
- `/search?q=keyword`
- `/about`
- `/contact`
- `/privacy`
- `/terms`

## Admin Pages

- `/admin/login`
- `/admin/logout`
- `/admin/dashboard`
- `/admin/posts`
- `/admin/posts/create`
- `/admin/posts/{id}/edit`
- `/admin/categories`
- `/admin/tags`
- `/admin/pages`
- `/admin/settings`

## Media Upload Integration

Featured image uploads in the post editor are sent to:

```text
MEDIA_API_BASE_URL + MEDIA_UPLOAD_ENDPOINT
```

The app expects JSON with `secure_url` and stores that URL as the post featured image. If the media API is unavailable, the admin form displays a clear error and does not crash.

## Tests

Run terminal tests:

```bash
pytest
```

Browser verification:

1. Start the server on port `6000`.
2. Open `http://127.0.0.1:6000`.
3. Open the side menu and test navigation.
4. Log in at `/admin/login`.
5. Create a category, tag, and published post.
6. Search for the post.
7. Open the post detail page.
8. Check category and tag pages.
9. Confirm the browser console has no errors.
