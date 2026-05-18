import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SECRET_KEY", "test-secret-key-change-me")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "change-this-password")
os.environ.setdefault("PORT", "6000")
os.environ.setdefault("APP_NAME", "Blog Website")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("MEDIA_API_BASE_URL", "http://127.0.0.1:8000")
os.environ.setdefault("MEDIA_UPLOAD_ENDPOINT", "/api/cloudinary/upload")

from app.core.database import init_db, seed_default_data
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def prepare_database():
    init_db()
    seed_default_data()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def admin_client(client):
    response = client.post("/admin/login", data={"username": "admin", "password": "change-this-password"}, follow_redirects=False)
    assert response.status_code == 303
    return client
