import os
from pathlib import Path


for line in Path(".env").read_text().splitlines() if Path(".env").exists() else []:
    if not line or line.lstrip().startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    os.environ.setdefault(key.strip(), value.strip())


class Settings:
    app_name: str = os.getenv("APP_NAME", "NekoTunnel Central")
    admin_token: str = os.getenv("ADMIN_TOKEN", "change-me")
    app_secret: str = os.getenv("APP_SECRET", "")
    session_secret: str = os.getenv("APP_SECRET", os.getenv("SESSION_SECRET", "dev-session-secret-change-me"))
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", "3600"))
    cleanup_interval_seconds: int = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "300"))
    tunnel_session_ttl_seconds: int = int(os.getenv("TUNNEL_SESSION_TTL_SECONDS", os.getenv("SESSION_TTL_SECONDS", "90")))
    tunnel_cleanup_interval_seconds: int = int(os.getenv("TUNNEL_CLEANUP_INTERVAL_SECONDS", "30"))
    database_url: str | None = os.getenv("DATABASE_URL")
    public_base_url: str | None = os.getenv("PUBLIC_BASE_URL")
    railway_cli_path: str | None = os.getenv("RAILWAY_CLI_PATH")
    railway_home_dir: str = os.getenv("RAILWAY_HOME_DIR", "/tmp/railway")
    render: bool = bool(os.getenv("RENDER"))


settings = Settings()
