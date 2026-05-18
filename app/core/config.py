from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    port: int = 6000
    app_name: str = "Blog Website"
    app_env: str = "development"
    secret_key: str = Field(min_length=12)
    database_url: str
    media_api_base_url: str = "http://127.0.0.1:8000"
    media_upload_endpoint: str = "/api/cloudinary/upload"
    admin_username: str = "admin"
    admin_email: str = "admin@example.com"
    admin_password: str = Field(min_length=8)

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("database_url")
    @classmethod
    def require_postgresql(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
            raise ValueError("DATABASE_URL must be a PostgreSQL connection string")
        return value.replace("postgresql://", "postgresql+psycopg://", 1)

    @property
    def media_upload_url(self) -> str:
        return f"{self.media_api_base_url.rstrip('/')}/{self.media_upload_endpoint.lstrip('/')}"

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
