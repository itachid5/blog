from collections.abc import Generator
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings
from app.core.security import hash_password

settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_menu_columns()


def ensure_menu_columns() -> None:
    inspector = inspect(engine)
    expected = {
        "show_in_menu": "BOOLEAN NOT NULL DEFAULT FALSE",
        "menu_order": "INTEGER NOT NULL DEFAULT 0",
    }
    with engine.begin() as connection:
        for table_name in ("categories", "tags"):
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, column_definition in expected.items():
                if column_name not in existing:
                    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"))


def seed_default_data() -> None:
    from app.models.page import Page
    from app.models.setting import Setting
    from app.models.user import User

    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.email == settings.admin_email))
        if admin is None:
            admin = User(
                username=settings.admin_username,
                email=settings.admin_email,
                hashed_password=hash_password(settings.admin_password),
                role="admin",
                is_active=True,
            )
            db.add(admin)

        defaults = {
            "site_name": settings.app_name,
            "site_description": "A modern professional blog built with FastAPI.",
            "logo_url": "",
            "twitter_url": "",
            "github_url": "",
            "linkedin_url": "",
        }
        for key, value in defaults.items():
            if db.scalar(select(Setting).where(Setting.key == key)) is None:
                db.add(Setting(key=key, value=value))

        pages = {
            "about": ("About", "Welcome to our blog. This page can be edited from the admin panel."),
            "contact": ("Contact", "Contact us using your preferred email or social channel."),
            "privacy": ("Privacy Policy", "This privacy policy explains how this website handles information."),
            "terms": ("Terms and Conditions", "These terms describe acceptable use of this website."),
        }
        now = datetime.now(timezone.utc)
        for slug, (title, content) in pages.items():
            if db.scalar(select(Page).where(Page.slug == slug)) is None:
                db.add(Page(title=title, slug=slug, content=content, status="published", created_at=now, updated_at=now))

        db.commit()
    finally:
        db.close()
