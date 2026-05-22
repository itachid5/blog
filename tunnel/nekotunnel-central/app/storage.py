import hashlib
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

try:
    import psycopg2
    from psycopg2.extras import DictCursor
except ImportError:
    psycopg2 = None
    DictCursor = None

from .models import AuditLog, ProvisionLog, RailwayAccount, RailwayBillingSnapshot, RailwayCliLogin, RailwayProject, Slot, TunnelSession, UserToken


DB_PATH = Path("data/nekotunnel.db")
EXPECTED_TABLES = (
    "railway_accounts",
    "railway_projects",
    "slots",
    "users",
    "sessions",
    "audit_logs",
    "provision_logs",
    "railway_cli_logins",
    "railway_cli_session_backups",
    "railway_billing_snapshots",
    "app_settings",
)
SEQUENCE_TABLES = {"railway_accounts", "railway_projects", "slots", "users", "audit_logs", "provision_logs", "railway_cli_logins", "railway_cli_session_backups", "railway_billing_snapshots"}
INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_users_token_hash ON users(token_hash)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user_status ON sessions(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_status_heartbeat ON sessions(status, last_heartbeat_at)",
    "CREATE INDEX IF NOT EXISTS idx_slots_status_tcp ON slots(status, tcp_status)",
    "CREATE INDEX IF NOT EXISTS idx_slots_current_session ON slots(current_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_slots_railway_project ON slots(railway_project_id)",
    "CREATE INDEX IF NOT EXISTS idx_slots_railway_account ON slots(railway_account_id)",
    "CREATE INDEX IF NOT EXISTS idx_projects_railway_account ON railway_projects(railway_account_id)",
    "CREATE INDEX IF NOT EXISTS idx_billing_snapshots_account_created ON railway_billing_snapshots(account_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_provision_logs_status_created ON provision_logs(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_provision_logs_action_created ON provision_logs(action, created_at)",
)


def database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def database_type() -> str:
    value = database_url().lower()
    return "postgres" if value.startswith(("postgres://", "postgresql://")) else "sqlite"


class PostgresCursor:
    def __init__(self, cursor, lastrowid: int | None = None) -> None:
        self.cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return self.cursor.rowcount

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()


class PostgresConnection:
    def __init__(self, dsn: str) -> None:
        if psycopg2 is None or DictCursor is None:
            raise RuntimeError("PostgreSQL requires psycopg2-binary to be installed.")
        self.conn = psycopg2.connect(dsn, cursor_factory=DictCursor)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type:
            self.conn.rollback()
        self.conn.close()

    def execute(self, sql: str, params: tuple | list | None = None) -> PostgresCursor:
        converted = self.convert_sql(sql)
        cursor = self.conn.cursor()
        cursor.execute(converted, tuple(params or ()))
        lastrowid = self.last_insert_id(sql)
        return PostgresCursor(cursor, lastrowid)

    def last_insert_id(self, sql: str) -> int | None:
        match = re.match(r"\s*INSERT\s+INTO\s+([a-z_]+)", sql, re.IGNORECASE)
        if not match or match.group(1).lower() not in SEQUENCE_TABLES:
            return None
        cursor = self.conn.cursor()
        cursor.execute("SELECT LASTVAL()")
        row = cursor.fetchone()
        return int(row[0]) if row else None

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def convert_sql(sql: str) -> str:
        converted = sql.replace("?", "%s")
        return converted


class SQLiteStore:
    database_type = "sqlite"

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self.database_url_present = bool(database_url())
        self.migration_status = "not initialized"
        self.connection_ok = False
        self.table_count = 0

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS railway_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label TEXT NOT NULL,
                    workspace_override TEXT,
                    token_encrypted_or_masked TEXT NOT NULL DEFAULT '',
                    token_prefix TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'unchecked',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    auth_type TEXT NOT NULL DEFAULT 'token',
                    cli_backup_present INTEGER NOT NULL DEFAULT 0,
                    cli_backup_updated_at TEXT,
                    cli_backup_size_bytes INTEGER,
                    manual_plan_name TEXT,
                    manual_subscription_status TEXT,
                    manual_credits_total TEXT,
                    manual_credits_remaining TEXT,
                    manual_billing_started_at TEXT,
                    manual_billing_renews_at TEXT,
                    manual_billing_days INTEGER,
                    manual_billing_note TEXT,
                    manual_billing_enabled INTEGER NOT NULL DEFAULT 0,
                    manual_billing_updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS railway_projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    railway_account_id INTEGER,
                    project_name TEXT NOT NULL,
                    project_id TEXT,
                    environment_id TEXT,
                    workdir TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'project_created',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (railway_account_id) REFERENCES railway_accounts(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    railway_account_id INTEGER,
                    project_name TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    server_addr TEXT NOT NULL DEFAULT '',
                    server_port TEXT NOT NULL DEFAULT '',
                    frp_token_encrypted_or_masked TEXT NOT NULL DEFAULT '',
                    frp_token_prefix TEXT NOT NULL DEFAULT '',
                    remote_port INTEGER NOT NULL DEFAULT 6000,
                    status TEXT NOT NULL DEFAULT 'free',
                    error TEXT NOT NULL DEFAULT '',
                    workdir TEXT,
                    frp_token_hash_or_encrypted TEXT,
                    deploy_status TEXT,
                    deployment_id TEXT,
                    last_deployed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (railway_account_id) REFERENCES railway_accounts(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    token_prefix TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    max_sessions INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER,
                    slot_id INTEGER,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_heartbeat_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ended_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY (slot_id) REFERENCES slots(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS provision_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    railway_account_id INTEGER,
                    slot_id INTEGER,
                    action TEXT NOT NULL,
                    account_label TEXT NOT NULL DEFAULT '',
                    project_name TEXT NOT NULL,
                    service_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    command TEXT NOT NULL DEFAULT '',
                    stdout TEXT NOT NULL DEFAULT '',
                    stderr TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (railway_account_id) REFERENCES railway_accounts(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS railway_cli_logins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    login_url TEXT,
                    pairing_code TEXT,
                    stdout TEXT NOT NULL DEFAULT '',
                    stderr TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT,
                    FOREIGN KEY (account_id) REFERENCES railway_accounts(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS railway_cli_session_backups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    encrypted_blob TEXT NOT NULL,
                    sha256 TEXT,
                    size_bytes INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    restored_at TEXT,
                    error TEXT,
                    FOREIGN KEY (account_id) REFERENCES railway_accounts(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS railway_billing_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    plan_name TEXT,
                    subscription_status TEXT,
                    credits_remaining TEXT,
                    current_usage TEXT,
                    billing_period_start TEXT,
                    billing_period_end TEXT,
                    trial_expires_at TEXT,
                    promo_expires_at TEXT,
                    raw_summary TEXT,
                    discovery_status TEXT NOT NULL DEFAULT 'unavailable',
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (account_id) REFERENCES railway_accounts(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            self.ensure_railway_account_columns(conn)
            self.ensure_railway_cli_login_columns(conn)
            self.ensure_cli_session_backup_schema(conn)
            self.ensure_railway_billing_snapshot_schema(conn)
            self.ensure_railway_project_columns(conn)
            self.ensure_slot_columns(conn)
            self.ensure_session_columns(conn)
            self.ensure_provision_log_columns(conn)
            self.create_indexes(conn)
            conn.commit()
            self.connection_ok = True
            self.table_count = self.count_tables(conn)
            self.migration_status = "ready"
        self.add_log("system", "db.startup", f"SQLite ready at {self.db_path}")

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def order_by_timestamp(self, column: str, direction: str = "DESC") -> str:
        direction = direction.upper()
        if direction not in {"ASC", "DESC"}:
            raise ValueError("direction must be ASC or DESC")
        if self.database_type == "postgres":
            return f"NULLIF({column}, '')::timestamptz {direction} NULLS LAST"
        return f"datetime({column}) {direction}"

    def table_columns(self, conn, table_name: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}

    def count_tables(self, conn) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN ({})".format(
                ",".join("?" for _ in EXPECTED_TABLES)
            ),
            EXPECTED_TABLES,
        ).fetchone()[0]

    def create_indexes(self, conn) -> None:
        for statement in INDEX_STATEMENTS:
            conn.execute(statement)

    def database_info(self) -> dict[str, object]:
        return {
            "type": self.database_type,
            "connected": self.connection_ok,
            "table_count": self.table_count,
            "expected_table_count": len(EXPECTED_TABLES),
            "migration_status": self.migration_status,
            "database_url_present": self.database_url_present,
            "sqlite_path": str(self.db_path),
        }

    def ensure_railway_account_columns(self, conn: sqlite3.Connection) -> None:
        columns = self.table_columns(conn, "railway_accounts")
        definitions = {
            "token_encrypted_or_masked": "TEXT NOT NULL DEFAULT ''",
            "token_prefix": "TEXT NOT NULL DEFAULT ''",
            "auth_type": "TEXT NOT NULL DEFAULT 'token'",
            "cli_backup_present": "INTEGER NOT NULL DEFAULT 0",
            "cli_backup_updated_at": "TEXT",
            "cli_backup_size_bytes": "INTEGER",
            "manual_plan_name": "TEXT",
            "manual_subscription_status": "TEXT",
            "manual_credits_total": "TEXT",
            "manual_credits_remaining": "TEXT",
            "manual_billing_started_at": "TEXT",
            "manual_billing_renews_at": "TEXT",
            "manual_billing_days": "INTEGER",
            "manual_billing_note": "TEXT",
            "manual_billing_enabled": "INTEGER NOT NULL DEFAULT 0",
            "manual_billing_updated_at": "TEXT",
        }
        for name, definition in definitions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE railway_accounts ADD COLUMN {name} {definition}")

    def ensure_railway_cli_login_columns(self, conn: sqlite3.Connection) -> None:
        if self.database_type == "postgres":
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS railway_cli_logins (
                    id SERIAL PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES railway_accounts(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    login_url TEXT,
                    pairing_code TEXT,
                    stdout TEXT NOT NULL DEFAULT '',
                    stderr TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                    completed_at TEXT
                )
                """
            )
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS railway_cli_logins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    login_url TEXT,
                    pairing_code TEXT,
                    stdout TEXT NOT NULL DEFAULT '',
                    stderr TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT,
                    FOREIGN KEY (account_id) REFERENCES railway_accounts(id) ON DELETE CASCADE
                )
                """
            )
        columns = self.table_columns(conn, "railway_cli_logins")
        definitions = {
            "account_id": "INTEGER NOT NULL DEFAULT 0",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "login_url": "TEXT",
            "pairing_code": "TEXT",
            "stdout": "TEXT NOT NULL DEFAULT ''",
            "stderr": "TEXT NOT NULL DEFAULT ''",
            "error": "TEXT NOT NULL DEFAULT ''",
            "started_at": "TEXT NOT NULL DEFAULT ''",
            "completed_at": "TEXT",
        }
        for name, definition in definitions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE railway_cli_logins ADD COLUMN {name} {definition}")

    def ensure_cli_session_backup_schema(self, conn: sqlite3.Connection) -> None:
        if self.database_type == "postgres":
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS railway_cli_session_backups (
                    id SERIAL PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES railway_accounts(id) ON DELETE CASCADE,
                    encrypted_blob TEXT NOT NULL,
                    sha256 TEXT,
                    size_bytes INTEGER,
                    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                    restored_at TEXT,
                    error TEXT
                )
                """
            )
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS railway_cli_session_backups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    encrypted_blob TEXT NOT NULL,
                    sha256 TEXT,
                    size_bytes INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    restored_at TEXT,
                    error TEXT,
                    FOREIGN KEY (account_id) REFERENCES railway_accounts(id) ON DELETE CASCADE
                )
                """
            )
        columns = self.table_columns(conn, "railway_cli_session_backups")
        definitions = {
            "account_id": "INTEGER NOT NULL DEFAULT 0",
            "encrypted_blob": "TEXT NOT NULL DEFAULT ''",
            "sha256": "TEXT",
            "size_bytes": "INTEGER",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
            "restored_at": "TEXT",
            "error": "TEXT",
        }
        for name, definition in definitions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE railway_cli_session_backups ADD COLUMN {name} {definition}")

    def ensure_railway_billing_snapshot_schema(self, conn: sqlite3.Connection) -> None:
        if self.database_type == "postgres":
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS railway_billing_snapshots (
                    id SERIAL PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES railway_accounts(id) ON DELETE CASCADE,
                    plan_name TEXT,
                    subscription_status TEXT,
                    credits_remaining TEXT,
                    current_usage TEXT,
                    billing_period_start TEXT,
                    billing_period_end TEXT,
                    trial_expires_at TEXT,
                    promo_expires_at TEXT,
                    raw_summary TEXT,
                    discovery_status TEXT NOT NULL DEFAULT 'unavailable',
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
                )
                """
            )
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS railway_billing_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    plan_name TEXT,
                    subscription_status TEXT,
                    credits_remaining TEXT,
                    current_usage TEXT,
                    billing_period_start TEXT,
                    billing_period_end TEXT,
                    trial_expires_at TEXT,
                    promo_expires_at TEXT,
                    raw_summary TEXT,
                    discovery_status TEXT NOT NULL DEFAULT 'unavailable',
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (account_id) REFERENCES railway_accounts(id) ON DELETE CASCADE
                )
                """
            )
        columns = self.table_columns(conn, "railway_billing_snapshots")
        definitions = {
            "account_id": "INTEGER NOT NULL DEFAULT 0",
            "plan_name": "TEXT",
            "subscription_status": "TEXT",
            "credits_remaining": "TEXT",
            "current_usage": "TEXT",
            "billing_period_start": "TEXT",
            "billing_period_end": "TEXT",
            "trial_expires_at": "TEXT",
            "promo_expires_at": "TEXT",
            "raw_summary": "TEXT",
            "discovery_status": "TEXT NOT NULL DEFAULT 'unavailable'",
            "error": "TEXT",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in definitions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE railway_billing_snapshots ADD COLUMN {name} {definition}")

    def ensure_railway_project_columns(self, conn: sqlite3.Connection) -> None:
        columns = self.table_columns(conn, "railway_projects")
        definitions = {
            "railway_account_id": "INTEGER",
            "project_name": "TEXT NOT NULL DEFAULT ''",
            "project_id": "TEXT",
            "environment_id": "TEXT",
            "workdir": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'project_created'",
            "error": "TEXT NOT NULL DEFAULT ''",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in definitions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE railway_projects ADD COLUMN {name} {definition}")

    def ensure_slot_columns(self, conn: sqlite3.Connection) -> None:
        columns = self.table_columns(conn, "slots")
        definitions = {
            "workdir": "TEXT",
            "frp_token_hash_or_encrypted": "TEXT",
            "frp_token_prefix": "TEXT",
            "deploy_status": "TEXT",
            "deployment_id": "TEXT",
            "last_deployed_at": "TEXT",
            "tcp_status": "TEXT",
            "tcp_last_checked_at": "TEXT",
            "project_id": "TEXT",
            "environment_id": "TEXT",
            "service_id": "TEXT",
            "service_instance_id": "TEXT",
            "railway_project_id": "INTEGER",
            "current_session_id": "TEXT",
        }
        for name, definition in definitions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE slots ADD COLUMN {name} {definition}")

    def ensure_session_columns(self, conn: sqlite3.Connection) -> None:
        columns = self.table_columns(conn, "sessions")
        definitions = {
            "client_info": "TEXT NOT NULL DEFAULT ''",
            "proxy_name": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in definitions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")

    def ensure_provision_log_columns(self, conn: sqlite3.Connection) -> None:
        columns = self.table_columns(conn, "provision_logs")
        definitions = {
            "railway_account_id": "INTEGER",
            "slot_id": "INTEGER",
            "action": "TEXT NOT NULL DEFAULT ''",
            "account_label": "TEXT NOT NULL DEFAULT ''",
            "project_name": "TEXT NOT NULL DEFAULT ''",
            "service_name": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'failed'",
            "command": "TEXT NOT NULL DEFAULT ''",
            "stdout": "TEXT NOT NULL DEFAULT ''",
            "stderr": "TEXT NOT NULL DEFAULT ''",
            "error": "TEXT NOT NULL DEFAULT ''",
            "duration_ms": "INTEGER NOT NULL DEFAULT 0",
            "created_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in definitions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE provision_logs ADD COLUMN {name} {definition}")

    @property
    def railway_accounts(self) -> list[RailwayAccount]:
        return self.list_railway_accounts()

    @property
    def projects(self) -> list[RailwayProject]:
        return self.list_railway_projects()

    @property
    def slots(self) -> list[Slot]:
        return self.list_slots()

    @property
    def users(self) -> list[UserToken]:
        return self.list_users()

    @property
    def sessions(self) -> list[TunnelSession]:
        return self.list_sessions()

    @property
    def logs(self) -> list[AuditLog]:
        return self.list_logs()

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                "total_accounts": conn.execute("SELECT COUNT(*) FROM railway_accounts").fetchone()[0],
                "total_projects": conn.execute("SELECT COUNT(*) FROM railway_projects").fetchone()[0],
                "total_slots": conn.execute("SELECT COUNT(*) FROM slots").fetchone()[0],
                "free_slots": conn.execute("SELECT COUNT(*) FROM slots WHERE status = 'free'").fetchone()[0],
                "busy_slots": conn.execute("SELECT COUNT(*) FROM slots WHERE status = 'busy'").fetchone()[0],
                "active_sessions": conn.execute("SELECT COUNT(*) FROM sessions WHERE status = 'active'").fetchone()[0],
                "failed_slots": conn.execute("SELECT COUNT(*) FROM slots WHERE status IN ('failed', 'deploy_failed') OR tcp_status = 'failed'").fetchone()[0],
                "project_created_slots": conn.execute("SELECT COUNT(*) FROM slots WHERE status = 'project_created'").fetchone()[0],
                "deployed_slots": conn.execute("SELECT COUNT(*) FROM slots WHERE status = 'deployed'").fetchone()[0],
                "deploy_failed_slots": conn.execute("SELECT COUNT(*) FROM slots WHERE status = 'deploy_failed'").fetchone()[0],
                "tcp_ready_slots": conn.execute("SELECT COUNT(*) FROM slots WHERE tcp_status = 'ready'").fetchone()[0],
                "tcp_pending_slots": conn.execute("SELECT COUNT(*) FROM slots WHERE status = 'tcp_pending' OR tcp_status = 'pending'").fetchone()[0],
            }

    def add_log(self, actor: str, action: str, details: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_logs (actor, action, details) VALUES (?, ?, ?)",
                (actor, action, details),
            )
            conn.commit()

    def list_logs(self) -> list[AuditLog]:
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM audit_logs ORDER BY {self.order_by_timestamp('created_at')}, id DESC").fetchall()
        return [AuditLog(**dict(row)) for row in rows]

    def add_provision_log(
        self,
        railway_account_id: int | None,
        action: str,
        project_name: str,
        status: str,
        command: str,
        stdout: str,
        stderr: str,
        error: str,
        duration_ms: int,
        slot_id: int | None = None,
        account_label: str = "",
        service_name: str = "",
    ) -> ProvisionLog:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO provision_logs (
                    railway_account_id, slot_id, action, account_label, project_name, service_name,
                    status, command, stdout, stderr, error, duration_ms
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    railway_account_id,
                    slot_id,
                    action,
                    account_label,
                    project_name,
                    service_name,
                    status,
                    command,
                    stdout,
                    stderr,
                    error,
                    duration_ms,
                ),
            )
            conn.commit()
            log_id = cursor.lastrowid
        self.add_log("admin", "provision_log.create", f"{action} {status} for {project_name}/{service_name or '-'}")
        return self.get_provision_log(log_id)

    def get_provision_log(self, log_id: int) -> ProvisionLog | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM provision_logs WHERE id = ?", (log_id,)).fetchone()
        return ProvisionLog(**dict(row)) if row else None

    def list_provision_logs(self, filter_value: str | None = None) -> list[ProvisionLog]:
        with self.connect() as conn:
            if filter_value in {"success", "failed", "running"}:
                rows = conn.execute(
                    f"SELECT * FROM provision_logs WHERE status = ? ORDER BY {self.order_by_timestamp('created_at')}, id DESC",
                    (filter_value,),
                ).fetchall()
            elif filter_value in {"create_project", "project_created", "create_service", "deploy_service", "redeploy_service", "refresh_tcp", "write_files"}:
                action = "create_project" if filter_value in {"create_project", "project_created"} else filter_value
                rows = conn.execute(
                    f"SELECT * FROM provision_logs WHERE action = ? ORDER BY {self.order_by_timestamp('created_at')}, id DESC",
                    (action,),
                ).fetchall()
            else:
                rows = conn.execute(f"SELECT * FROM provision_logs ORDER BY {self.order_by_timestamp('created_at')}, id DESC").fetchall()
        return [ProvisionLog(**dict(row)) for row in rows]

    def latest_successful_project_stdout(self, project_name: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT stdout FROM provision_logs
                WHERE project_name = ? AND action = 'railway_project_create' AND status = 'success'
                ORDER BY {self.order_by_timestamp('created_at')}, id DESC
                LIMIT 1
                """,
                (project_name,),
            ).fetchone()
        return row[0] if row else ""

    def delete_provision_log(self, log_id: int) -> bool:
        log = self.get_provision_log(log_id)
        if not log:
            return False
        with self.connect() as conn:
            conn.execute("DELETE FROM provision_logs WHERE id = ?", (log_id,))
            conn.commit()
        self.add_log("admin", "provision_log.delete", f"Deleted provision log {log_id}")
        return True

    def add_railway_account(self, label: str, token: str = "", workspace: str | None = None, auth_type: str = "token") -> RailwayAccount:
        normalized_auth_type = auth_type if auth_type in {"token", "cli_session"} else "token"
        token_value = token if normalized_auth_type == "token" else ""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO railway_accounts (
                    label, workspace_override, token_encrypted_or_masked, token_prefix, status, error, auth_type
                )
                VALUES (?, ?, ?, ?, 'unchecked', '', ?)
                """,
                (label, workspace or None, token_value, token_prefix(token_value) if token_value else "", normalized_auth_type),
            )
            conn.commit()
            account_id = cursor.lastrowid
        self.add_log("admin", "railway_account.add", f"Added {normalized_auth_type} account {label}")
        return self.get_account(account_id)

    def list_railway_accounts(self) -> list[RailwayAccount]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM railway_accounts ORDER BY id DESC").fetchall()
        return [RailwayAccount(**dict(row)) for row in rows]

    def get_account(self, account_id: int) -> RailwayAccount | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM railway_accounts WHERE id = ?", (account_id,)).fetchone()
        return RailwayAccount(**dict(row)) if row else None

    def update_manual_billing(
        self,
        account_id: int,
        manual_billing_enabled: int,
        manual_plan_name: str | None,
        manual_subscription_status: str | None,
        manual_credits_total: str | None,
        manual_credits_remaining: str | None,
        manual_billing_started_at: str | None,
        manual_billing_renews_at: str | None,
        manual_billing_days: int | None,
        manual_billing_note: str | None,
    ) -> bool:
        # Manual billing tracker is display-only.
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE railway_accounts
                SET manual_billing_enabled = ?,
                    manual_plan_name = ?,
                    manual_subscription_status = ?,
                    manual_credits_total = ?,
                    manual_credits_remaining = ?,
                    manual_billing_started_at = ?,
                    manual_billing_renews_at = ?,
                    manual_billing_days = ?,
                    manual_billing_note = ?,
                    manual_billing_updated_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    manual_billing_enabled,
                    manual_plan_name,
                    manual_subscription_status,
                    manual_credits_total,
                    manual_credits_remaining,
                    manual_billing_started_at,
                    manual_billing_renews_at,
                    manual_billing_days,
                    manual_billing_note,
                    account_id,
                ),
            )
            conn.commit()
        return cursor.rowcount > 0

    def update_railway_account_status(self, account_id: int, status: str, error: str = "") -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE railway_accounts
                SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, error[:500], account_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def save_cli_session_backup(self, account_id: int, encrypted_blob: str, sha256: str, size_bytes: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM railway_cli_session_backups WHERE account_id = ?", (account_id,))
            conn.execute(
                """
                INSERT INTO railway_cli_session_backups (account_id, encrypted_blob, sha256, size_bytes, error)
                VALUES (?, ?, ?, ?, '')
                """,
                (account_id, encrypted_blob, sha256, size_bytes),
            )
            conn.execute(
                """
                UPDATE railway_accounts
                SET cli_backup_present = 1,
                    cli_backup_updated_at = CURRENT_TIMESTAMP,
                    cli_backup_size_bytes = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (size_bytes, account_id),
            )
            conn.commit()
        self.add_log("admin", "railway_cli_session_backup.save", f"Saved encrypted CLI session backup for account {account_id} ({size_bytes} bytes)")

    def latest_cli_session_backup(self, account_id: int):
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM railway_cli_session_backups
                WHERE account_id = ?
                ORDER BY {self.order_by_timestamp('updated_at')}, id DESC
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        return dict(row) if row else None

    def mark_cli_session_backup_restored(self, account_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE railway_cli_session_backups
                SET restored_at = CURRENT_TIMESTAMP, error = ''
                WHERE id = (
                    SELECT id FROM railway_cli_session_backups WHERE account_id = ? ORDER BY id DESC LIMIT 1
                )
                """,
                (account_id,),
            )
            conn.commit()

    def mark_cli_session_backup_error(self, account_id: int, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE railway_cli_session_backups
                SET error = ?
                WHERE id = (
                    SELECT id FROM railway_cli_session_backups WHERE account_id = ? ORDER BY id DESC LIMIT 1
                )
                """,
                (error[:500], account_id),
            )
            conn.commit()

    def delete_cli_session_backup(self, account_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM railway_cli_session_backups WHERE account_id = ?", (account_id,))
            conn.execute(
                """
                UPDATE railway_accounts
                SET cli_backup_present = 0,
                    cli_backup_updated_at = NULL,
                    cli_backup_size_bytes = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (account_id,),
            )
            conn.commit()
        self.add_log("admin", "railway_cli_session_backup.delete", f"Deleted encrypted CLI session backup for account {account_id}")
        return cursor.rowcount > 0

    def cli_session_backup_stats(self) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS total_size FROM railway_cli_session_backups").fetchone()
        return {"count": int(row["count"] if row else 0), "total_size": int(row["total_size"] if row else 0)}

    def save_railway_billing_snapshot(
        self,
        account_id: int,
        plan_name: str | None = None,
        subscription_status: str | None = None,
        credits_remaining: str | None = None,
        current_usage: str | None = None,
        billing_period_start: str | None = None,
        billing_period_end: str | None = None,
        trial_expires_at: str | None = None,
        promo_expires_at: str | None = None,
        raw_summary: str | None = None,
        discovery_status: str = "unavailable",
        error: str | None = None,
    ) -> RailwayBillingSnapshot:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO railway_billing_snapshots (
                    account_id, plan_name, subscription_status, credits_remaining, current_usage,
                    billing_period_start, billing_period_end, trial_expires_at, promo_expires_at,
                    raw_summary, discovery_status, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    plan_name,
                    subscription_status,
                    credits_remaining,
                    current_usage,
                    billing_period_start,
                    billing_period_end,
                    trial_expires_at,
                    promo_expires_at,
                    raw_summary,
                    discovery_status,
                    error[:1000] if error else None,
                ),
            )
            conn.commit()
            snapshot_id = cursor.lastrowid
        self.add_log("admin", "railway_billing_snapshot.save", f"Saved billing discovery snapshot for account {account_id}: {discovery_status}")
        return self.get_railway_billing_snapshot(snapshot_id)

    def get_railway_billing_snapshot(self, snapshot_id: int) -> RailwayBillingSnapshot | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM railway_billing_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        return RailwayBillingSnapshot(**dict(row)) if row else None

    def latest_railway_billing_snapshot(self, account_id: int) -> RailwayBillingSnapshot | None:
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM railway_billing_snapshots
                WHERE account_id = ?
                ORDER BY {self.order_by_timestamp('created_at')}, id DESC
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        return RailwayBillingSnapshot(**dict(row)) if row else None

    def latest_billing_snapshots_by_account(self) -> dict[int, RailwayBillingSnapshot]:
        snapshots: dict[int, RailwayBillingSnapshot] = {}
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM railway_billing_snapshots ORDER BY {self.order_by_timestamp('created_at')}, id DESC"
            ).fetchall()
        for row in rows:
            snapshot = RailwayBillingSnapshot(**dict(row))
            snapshots.setdefault(snapshot.account_id, snapshot)
        return snapshots

    def list_railway_billing_snapshots(self, account_id: int | None = None, limit: int = 100) -> list[RailwayBillingSnapshot]:
        limit = max(1, min(int(limit), 500))
        with self.connect() as conn:
            if account_id is None:
                rows = conn.execute(
                    f"SELECT * FROM railway_billing_snapshots ORDER BY {self.order_by_timestamp('created_at')}, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT * FROM railway_billing_snapshots
                    WHERE account_id = ?
                    ORDER BY {self.order_by_timestamp('created_at')}, id DESC
                    LIMIT ?
                    """,
                    (account_id, limit),
                ).fetchall()
        return [RailwayBillingSnapshot(**dict(row)) for row in rows]

    def create_cli_login_attempt(self, account_id: int) -> RailwayCliLogin:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO railway_cli_logins (account_id, status, stdout, stderr, error)
                VALUES (?, 'pending', '', '', '')
                """,
                (account_id,),
            )
            conn.commit()
            login_id = cursor.lastrowid
        self.add_log("admin", "railway_cli_login.start", f"Started CLI login attempt {login_id} for account {account_id}")
        return self.get_cli_login_attempt(login_id)

    def get_cli_login_attempt(self, login_id: int) -> RailwayCliLogin | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM railway_cli_logins WHERE id = ?", (login_id,)).fetchone()
        return RailwayCliLogin(**dict(row)) if row else None

    def latest_cli_login_attempt(self, account_id: int) -> RailwayCliLogin | None:
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM railway_cli_logins
                WHERE account_id = ?
                ORDER BY {self.order_by_timestamp('started_at')}, id DESC
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        return RailwayCliLogin(**dict(row)) if row else None

    def update_cli_login_attempt(
        self,
        login_id: int,
        status: str | None = None,
        login_url: str | None = None,
        pairing_code: str | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        error: str | None = None,
        completed: bool = False,
    ) -> bool:
        with self.connect() as conn:
            current = conn.execute("SELECT * FROM railway_cli_logins WHERE id = ?", (login_id,)).fetchone()
            if not current:
                return False
            completed_at_sql = "completed_at = CURRENT_TIMESTAMP" if completed else "completed_at = completed_at"
            cursor = conn.execute(
                f"""
                UPDATE railway_cli_logins
                SET status = ?,
                    login_url = ?,
                    pairing_code = ?,
                    stdout = ?,
                    stderr = ?,
                    error = ?,
                    {completed_at_sql}
                WHERE id = ?
                """,
                (
                    status if status is not None else current["status"],
                    login_url if login_url is not None else current["login_url"],
                    pairing_code if pairing_code is not None else current["pairing_code"],
                    stdout if stdout is not None else current["stdout"],
                    stderr if stderr is not None else current["stderr"],
                    (error[:500] if error is not None else current["error"]),
                    login_id,
                ),
            )
            conn.commit()
        return cursor.rowcount > 0

    def disable_account(self, account_id: int) -> bool:
        account = self.get_account(account_id)
        if not account:
            return False
        with self.connect() as conn:
            conn.execute(
                "UPDATE railway_accounts SET status = 'disabled', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (account_id,),
            )
            conn.commit()
        self.add_log("admin", "railway_account.disable", f"Disabled account {account.label}")
        return True

    def delete_account(self, account_id: int) -> bool:
        account = self.get_account(account_id)
        if not account:
            return False
        with self.connect() as conn:
            conn.execute("DELETE FROM railway_accounts WHERE id = ?", (account_id,))
            conn.commit()
        self.add_log("admin", "railway_account.delete", f"Deleted account {account.label}")
        return True

    def add_railway_project(
        self,
        account_id: int | None,
        project_name: str,
        workdir: str,
        status: str = "project_created",
        error: str = "",
        project_id: str = "",
        environment_id: str = "",
    ) -> RailwayProject:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO railway_projects (railway_account_id, project_name, project_id, environment_id, workdir, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (account_id, project_name, project_id or None, environment_id or None, workdir, status, error[:500]),
            )
            conn.commit()
            project_row_id = cursor.lastrowid
        self.add_log("admin", "railway_project.create", f"Created local project record {project_name}")
        return self.get_railway_project(project_row_id)

    def list_railway_projects(self) -> list[RailwayProject]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM railway_projects ORDER BY id DESC").fetchall()
        return [RailwayProject(**dict(row)) for row in rows]

    def get_railway_project(self, project_id: int) -> RailwayProject | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM railway_projects WHERE id = ?", (project_id,)).fetchone()
        return RailwayProject(**dict(row)) if row else None

    def update_railway_project_status(self, project_id: int, status: str, error: str = "") -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE railway_projects
                SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, error[:500], project_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def update_railway_project_ids(self, project_id: int, railway_project_id: str = "", environment_id: str = "") -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE railway_projects
                SET project_id = COALESCE(NULLIF(?, ''), project_id),
                    environment_id = COALESCE(NULLIF(?, ''), environment_id),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (railway_project_id, environment_id, project_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def update_railway_project_workdir(self, project_id: int, workdir: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE railway_projects
                SET workdir = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (workdir, project_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def delete_railway_project(self, project_id: int) -> bool:
        project = self.get_railway_project(project_id)
        if not project:
            return False
        with self.connect() as conn:
            conn.execute("DELETE FROM railway_projects WHERE id = ?", (project_id,))
            conn.commit()
        self.add_log("admin", "railway_project.delete", f"Deleted local project record {project.project_name}")
        return True

    def service_count_for_project(self, project_id: int) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM slots WHERE railway_project_id = ?", (project_id,)).fetchone()[0]

    def service_name_exists(self, project_id: int, service_name: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM slots WHERE railway_project_id = ? AND lower(service_name) = lower(?) LIMIT 1",
                (project_id, service_name),
            ).fetchone() is not None

    def next_service_name(self, project_id: int) -> str:
        with self.connect() as conn:
            names = {
                row[0]
                for row in conn.execute("SELECT service_name FROM slots WHERE railway_project_id = ?", (project_id,)).fetchall()
            }
        index = 1
        while f"final-{index}" in names:
            index += 1
        return f"final-{index}"

    def project_service_counts(self) -> dict[int, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT railway_project_id, COUNT(*) FROM slots WHERE railway_project_id IS NOT NULL GROUP BY railway_project_id"
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def create_service_slot(self, project: RailwayProject, service_name: str, frp_token: str) -> Slot:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO slots (
                    railway_project_id, railway_account_id, project_name, service_name, server_addr, server_port,
                    frp_token_encrypted_or_masked, frp_token_prefix, frp_token_hash_or_encrypted,
                    remote_port, status, deploy_status, tcp_status, error, workdir
                )
                VALUES (?, ?, ?, ?, '', '', ?, ?, ?, 6000, 'deploying', NULL, 'pending', '', ?)
                """,
                (
                    project.id,
                    project.railway_account_id,
                    project.project_name,
                    service_name,
                    mask_token(frp_token),
                    token_prefix(frp_token),
                    frp_token,
                    project.workdir,
                ),
            )
            conn.commit()
            slot_id = cursor.lastrowid
        self.add_log("admin", "slot.service_create", f"Created service slot {project.project_name}/{service_name}")
        return self.get_slot(slot_id)

    def add_placeholder_slot(self, account_id: int | None, project_name: str, service_name: str) -> Slot:
        error = "Provisioning not implemented yet"
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO slots (railway_account_id, project_name, service_name, remote_port, status, error)
                VALUES (?, ?, ?, 6000, 'tcp_pending', ?)
                """,
                (account_id, project_name, service_name or "final", error),
            )
            conn.commit()
            slot_id = cursor.lastrowid
        self.add_log("admin", "slot.create_placeholder", f"Created placeholder slot {project_name}")
        return self.get_slot(slot_id)

    def add_project_created_slot(self, account_id: int | None, project_name: str, workdir: str | None = None) -> Slot:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO slots (
                    railway_account_id, project_name, service_name, server_addr, server_port,
                    frp_token_encrypted_or_masked, frp_token_prefix, remote_port, status, error, workdir
                )
                VALUES (?, ?, 'final', '', '', '', '', 6000, 'project_created', '', ?)
                """,
                (account_id, project_name, workdir),
            )
            conn.commit()
            slot_id = cursor.lastrowid
        self.add_log("admin", "slot.project_created", f"Created Railway project {project_name}")
        return self.get_slot(slot_id)

    def add_manual_slot(
        self,
        project_name: str,
        service_name: str,
        server_address: str,
        server_port: str,
        frp_token: str,
    ) -> Slot:
        masked = mask_token(frp_token)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO slots (
                    railway_account_id, project_name, service_name, server_addr, server_port,
                    frp_token_encrypted_or_masked, frp_token_prefix, frp_token_hash_or_encrypted, remote_port, status, error
                )
                VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, 6000, 'free', '')
                """,
                (project_name, service_name, server_address, server_port, masked, token_prefix(frp_token), frp_token),
            )
            conn.commit()
            slot_id = cursor.lastrowid
        self.add_log("admin", "slot.add_manual", f"Added manual slot {project_name}")
        return self.get_slot(slot_id)

    def list_slots(self, status_filter: str | None = None, project_id: int | None = None) -> list[Slot]:
        query = """
            SELECT slots.*, sessions.user_id AS current_user_id, users.name AS current_user_name
            FROM slots
            LEFT JOIN sessions ON sessions.id = slots.current_session_id
            LEFT JOIN users ON users.id = sessions.user_id
        """
        params: list[object] = []
        conditions: list[str] = []
        if project_id is not None:
            conditions.append("slots.railway_project_id = ?")
            params.append(project_id)
        if status_filter == "free":
            conditions.append("slots.status = 'free'")
        elif status_filter == "deployed":
            conditions.append("slots.status = 'deployed'")
        elif status_filter == "tcp_pending":
            conditions.append("(slots.status = 'tcp_pending' OR slots.tcp_status = 'pending')")
        elif status_filter == "busy":
            conditions.append("slots.status = 'busy'")
        elif status_filter == "failed":
            conditions.append("(slots.status IN ('failed', 'deploy_failed') OR slots.tcp_status = 'failed')")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY slots.id DESC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [Slot(**dict(row)) for row in rows]

    def get_slot(self, slot_id: int) -> Slot | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
        return Slot(**dict(row)) if row else None

    def set_slot_workdir(self, slot_id: int, workdir: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE slots SET workdir = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (workdir, slot_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def set_slot_frp_token(self, slot_id: int, token: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE slots
                SET frp_token_hash_or_encrypted = ?, frp_token_encrypted_or_masked = ?, frp_token_prefix = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (token, mask_token(token), token_prefix(token), slot_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def set_slot_railway_ids(
        self,
        slot_id: int,
        project_id: str = "",
        environment_id: str = "",
        service_id: str = "",
        service_instance_id: str = "",
    ) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE slots
                SET project_id = ?, environment_id = ?, service_id = ?, service_instance_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (project_id, environment_id, service_id, service_instance_id, slot_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def latest_tcp_auto_enable_result(self) -> str:
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT status, error, stdout FROM provision_logs
                WHERE action IN ('tcp_discover_ids', 'tcp_auto_enable_attempt')
                ORDER BY {self.order_by_timestamp('created_at')}, id DESC
                LIMIT 1
                """
            ).fetchone()
        if not row:
            return "No TCP auto-enable discovery has run yet."
        detail = row[1] or row[2] or "-"
        return f"{row[0]}: {detail[:300]}"

    def tcp_auto_enable_mode(self) -> str:
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM slots WHERE tcp_status = 'manual_required' LIMIT 1").fetchone():
                return "manual_required"
            if conn.execute("SELECT 1 FROM provision_logs WHERE action = 'tcp_auto_enable_attempt' AND status = 'success' LIMIT 1").fetchone():
                return "supported"
        return "unknown"

    def mark_slot_deployed(self, slot_id: int, service_name: str, deployment_id: str = "") -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE slots
                SET status = 'deployed', deploy_status = 'success', service_name = ?, deployment_id = ?,
                    tcp_status = COALESCE(tcp_status, 'pending'), server_addr = '', server_port = '',
                    last_deployed_at = CURRENT_TIMESTAMP, error = '', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (service_name, deployment_id, slot_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def mark_slot_deploy_failed(self, slot_id: int, error: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE slots
                SET status = 'deploy_failed', deploy_status = 'failed', error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error[:500], slot_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def mark_slot_redeployed(self, slot_id: int, deployment_id: str = "") -> bool:
        slot = self.get_slot(slot_id)
        if not slot:
            return False
        if slot.status == "busy":
            status = "busy"
        elif slot.tcp_status == "ready" and slot.server_addr and slot.server_port:
            status = "free"
        else:
            status = "tcp_pending"
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE slots
                SET status = ?, deploy_status = 'success', deployment_id = COALESCE(NULLIF(?, ''), deployment_id),
                    last_deployed_at = CURRENT_TIMESTAMP, error = '', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, deployment_id, slot_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def mark_slot_tcp_ready(self, slot_id: int, server_addr: str, server_port: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE slots
                SET server_addr = ?, server_port = ?, status = 'free', tcp_status = 'ready',
                    tcp_last_checked_at = CURRENT_TIMESTAMP, error = '', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (server_addr, server_port, slot_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def mark_slot_tcp_pending(self, slot_id: int, error: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE slots
                SET status = 'tcp_pending', tcp_status = 'pending', tcp_last_checked_at = CURRENT_TIMESTAMP,
                    error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error[:500], slot_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def mark_slot_tcp_failed(self, slot_id: int, error: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE slots
                SET status = 'tcp_pending', tcp_status = 'failed', tcp_last_checked_at = CURRENT_TIMESTAMP,
                    error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error[:500], slot_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def mark_slot_tcp_manual_required(self, slot_id: int, error: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE slots
                SET status = 'tcp_pending', tcp_status = 'manual_required', tcp_last_checked_at = CURRENT_TIMESTAMP,
                    error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error[:500], slot_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def update_slot_status(self, slot_id: int, status: str) -> bool:
        slot = self.get_slot(slot_id)
        if not slot:
            return False
        error = "" if status == "free" else slot.error
        with self.connect() as conn:
            conn.execute(
                "UPDATE slots SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, error, slot_id),
            )
            conn.commit()
        self.add_log("admin", "slot.status", f"Set {slot.project_name} to {status}")
        return True

    def delete_slot(self, slot_id: int) -> bool:
        slot = self.get_slot(slot_id)
        if not slot:
            return False
        with self.connect() as conn:
            conn.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
            conn.commit()
        self.add_log("admin", "slot.delete", f"Deleted slot {slot.project_name}")
        return True

    def create_user_token(self, name: str, max_sessions: int) -> tuple[UserToken, str]:
        token = f"ntk_{secrets.token_urlsafe(28)}"
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO users (name, token_hash, token_prefix, status, max_sessions)
                VALUES (?, ?, ?, 'active', ?)
                """,
                (name, digest, mask_token(token), max_sessions),
            )
            conn.commit()
            user_id = cursor.lastrowid
        self.add_log("admin", "user_token.create", f"Created token for {name}")
        return self.get_user(user_id), token

    def list_users(self) -> list[UserToken]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
        return [UserToken(**dict(row)) for row in rows]

    def get_user(self, user_id: int) -> UserToken | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return UserToken(**dict(row)) if row else None

    def disable_user(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if not user:
            return False
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET status = 'disabled', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (user_id,),
            )
            conn.commit()
        self.add_log("admin", "user_token.disable", f"Disabled token for {user.name}")
        return True

    def delete_user(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if not user:
            return False
        with self.connect() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
        self.add_log("admin", "user_token.delete", f"Deleted token for {user.name}")
        return True

    def user_for_token(self, token: str) -> UserToken | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE token_hash = ?", (digest,)).fetchone()
        return UserToken(**dict(row)) if row else None

    def allocate_session(self, user: UserToken, local_port: int, client_info: str = "") -> tuple[dict | None, str]:
        session_id = secrets.token_urlsafe(16)
        proxy_name = f"nekotunnel-{session_id[:8]}"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active_count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id = ? AND status = 'active'",
                (user.id,),
            ).fetchone()[0]
            if active_count >= user.max_sessions:
                conn.rollback()
                return None, "max_sessions_reached"
            slot = conn.execute(
                """
                SELECT * FROM slots
                WHERE status = 'free'
                  AND tcp_status = 'ready'
                  AND COALESCE(server_addr, '') != ''
                  AND COALESCE(server_port, '') != ''
                  AND COALESCE(frp_token_hash_or_encrypted, '') != ''
                  AND COALESCE(frp_token_hash_or_encrypted, '') NOT LIKE '%...%'
                ORDER BY id ASC
                LIMIT 1
                """
            ).fetchone()
            if not slot:
                conn.rollback()
                return None, "no_free_slot"
            conn.execute(
                """
                INSERT INTO sessions (id, user_id, slot_id, status, client_info, proxy_name)
                VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (session_id, user.id, slot["id"], client_info[:500], proxy_name),
            )
            conn.execute(
                """
                UPDATE slots
                SET status = 'busy', current_session_id = ?, error = '', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'free'
                """,
                (session_id, slot["id"]),
            )
            conn.commit()
        self.add_log(
            "user",
            "connect",
            f"User {user.name} opened session {session_id} on slot {slot['id']} ({slot['project_name']}/{slot['service_name']}) for local port {local_port}",
        )
        return {
            "session_id": session_id,
            "slot_id": slot["id"],
            "server_addr": slot["server_addr"],
            "server_port": int(slot["server_port"]),
            "frp_token": slot["frp_token_hash_or_encrypted"],
            "remote_port": int(slot["remote_port"] or 6000),
            "proxy_name": proxy_name,
        }, ""

    def get_session_for_user(self, session_id: str, user_id: int) -> TunnelSession | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
        return TunnelSession(**dict(row)) if row else None

    def update_session_heartbeat(self, session_id: str, user_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sessions
                SET last_heartbeat_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ? AND status = 'active'
                """,
                (session_id, user_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def close_session(self, session_id: str, user_id: int | None = None, status: str = "closed", actor: str = "user") -> bool:
        with self.connect() as conn:
            if user_id is None:
                row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)).fetchone()
            if not row:
                return False
            conn.execute(
                """
                UPDATE sessions
                SET status = ?, ended_at = CURRENT_TIMESTAMP, last_heartbeat_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, session_id),
            )
            if row["slot_id"] is not None:
                conn.execute(
                    """
                    UPDATE slots
                    SET status = 'free', current_session_id = NULL, error = '', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND current_session_id = ?
                    """,
                    (row["slot_id"], session_id),
                )
            conn.commit()
        action = "heartbeat.timeout" if status == "expired" else "disconnect"
        self.add_log(actor, action, f"Released session {session_id} from slot {row['slot_id'] or '-'} with status {status}")
        return True

    def expire_stale_sessions(self, ttl_seconds: int) -> list[str]:
        cutoff = (datetime.utcnow() - timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM sessions
                WHERE status = 'active'
                  AND last_heartbeat_at < ?
                """,
                (cutoff,),
            ).fetchall()
        expired: list[str] = []
        for row in rows:
            if self.close_session(row["id"], None, "expired", "system"):
                expired.append(row["id"])
        return expired

    def force_release_slot(self, slot_id: int) -> bool:
        slot = self.get_slot(slot_id)
        if not slot:
            return False
        if slot.current_session_id:
            self.close_session(slot.current_session_id, None, "closed", "admin")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE slots
                SET status = 'free', current_session_id = NULL, error = '', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (slot_id,),
            )
            conn.commit()
        self.add_log("admin", "slot.force_release", f"Force released slot {slot_id}")
        return cursor.rowcount > 0

    def list_sessions(self) -> list[TunnelSession]:
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT sessions.*, users.name AS user_name,
                       slots.project_name || '/' || slots.service_name AS slot_label
                FROM sessions
                LEFT JOIN users ON users.id = sessions.user_id
                LEFT JOIN slots ON slots.id = sessions.slot_id
                ORDER BY {self.order_by_timestamp('sessions.started_at')}
                """
            ).fetchall()
        return [TunnelSession(**dict(row)) for row in rows]

    def force_close_session(self, session_id: str) -> bool:
        return self.close_session(session_id, None, "closed", "admin")


class PostgresStore(SQLiteStore):
    database_type = "postgres"

    def __init__(self, dsn: str) -> None:
        super().__init__(DB_PATH)
        self.dsn = dsn
        self.database_url_present = True

    def connect(self) -> PostgresConnection:
        return PostgresConnection(self.dsn)

    def init_db(self) -> None:
        with self.connect() as conn:
            for statement in self.schema_statements():
                conn.execute(statement)
            self.ensure_railway_account_columns(conn)
            self.ensure_railway_cli_login_columns(conn)
            self.ensure_cli_session_backup_schema(conn)
            self.ensure_railway_billing_snapshot_schema(conn)
            self.ensure_railway_project_columns(conn)
            self.ensure_slot_columns(conn)
            self.ensure_session_columns(conn)
            self.ensure_provision_log_columns(conn)
            self.create_indexes(conn)
            conn.commit()
            self.connection_ok = True
            self.table_count = self.count_tables(conn)
            self.migration_status = "ready"
        self.add_log("system", "db.startup", "PostgreSQL ready")

    def schema_statements(self) -> tuple[str, ...]:
        return (
            """
            CREATE TABLE IF NOT EXISTS railway_accounts (
                id SERIAL PRIMARY KEY,
                label TEXT NOT NULL,
                workspace_override TEXT,
                token_encrypted_or_masked TEXT NOT NULL DEFAULT '',
                token_prefix TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unchecked',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                auth_type TEXT NOT NULL DEFAULT 'token',
                cli_backup_present INTEGER NOT NULL DEFAULT 0,
                cli_backup_updated_at TEXT,
                cli_backup_size_bytes INTEGER,
                manual_plan_name TEXT,
                manual_subscription_status TEXT,
                manual_credits_total TEXT,
                manual_credits_remaining TEXT,
                manual_billing_started_at TEXT,
                manual_billing_renews_at TEXT,
                manual_billing_days INTEGER,
                manual_billing_note TEXT,
                manual_billing_enabled INTEGER NOT NULL DEFAULT 0,
                manual_billing_updated_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS railway_projects (
                id SERIAL PRIMARY KEY,
                railway_account_id INTEGER REFERENCES railway_accounts(id) ON DELETE SET NULL,
                project_name TEXT NOT NULL,
                project_id TEXT,
                environment_id TEXT,
                workdir TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'project_created',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS slots (
                id SERIAL PRIMARY KEY,
                railway_account_id INTEGER REFERENCES railway_accounts(id) ON DELETE SET NULL,
                project_name TEXT NOT NULL,
                service_name TEXT NOT NULL,
                server_addr TEXT NOT NULL DEFAULT '',
                server_port TEXT NOT NULL DEFAULT '',
                frp_token_encrypted_or_masked TEXT NOT NULL DEFAULT '',
                frp_token_prefix TEXT NOT NULL DEFAULT '',
                remote_port INTEGER NOT NULL DEFAULT 6000,
                status TEXT NOT NULL DEFAULT 'free',
                error TEXT NOT NULL DEFAULT '',
                workdir TEXT,
                frp_token_hash_or_encrypted TEXT,
                deploy_status TEXT,
                deployment_id TEXT,
                last_deployed_at TEXT,
                tcp_status TEXT,
                tcp_last_checked_at TEXT,
                project_id TEXT,
                environment_id TEXT,
                service_id TEXT,
                service_instance_id TEXT,
                railway_project_id INTEGER,
                current_session_id TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                token_prefix TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                max_sessions INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                slot_id INTEGER REFERENCES slots(id) ON DELETE SET NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                last_heartbeat_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                ended_at TEXT,
                client_info TEXT NOT NULL DEFAULT '',
                proxy_name TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id SERIAL PRIMARY KEY,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS provision_logs (
                id SERIAL PRIMARY KEY,
                railway_account_id INTEGER REFERENCES railway_accounts(id) ON DELETE SET NULL,
                slot_id INTEGER,
                action TEXT NOT NULL,
                account_label TEXT NOT NULL DEFAULT '',
                project_name TEXT NOT NULL,
                service_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                command TEXT NOT NULL DEFAULT '',
                stdout TEXT NOT NULL DEFAULT '',
                stderr TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS railway_cli_logins (
                id SERIAL PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES railway_accounts(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                login_url TEXT,
                pairing_code TEXT,
                stdout TEXT NOT NULL DEFAULT '',
                stderr TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                completed_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS railway_cli_session_backups (
                id SERIAL PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES railway_accounts(id) ON DELETE CASCADE,
                encrypted_blob TEXT NOT NULL,
                sha256 TEXT,
                size_bytes INTEGER,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                restored_at TEXT,
                error TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS railway_billing_snapshots (
                id SERIAL PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES railway_accounts(id) ON DELETE CASCADE,
                plan_name TEXT,
                subscription_status TEXT,
                credits_remaining TEXT,
                current_usage TEXT,
                billing_period_start TEXT,
                billing_period_end TEXT,
                trial_expires_at TEXT,
                promo_expires_at TEXT,
                raw_summary TEXT,
                discovery_status TEXT NOT NULL DEFAULT 'unavailable',
                error TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
            )
            """,
        )

    def table_columns(self, conn, table_name: str) -> set[str]:
        rows = conn.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ?
            """,
            (table_name,),
        ).fetchall()
        return {row[0] for row in rows}

    def count_tables(self, conn) -> int:
        rows = conn.execute(
            """
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = ANY(?::text[])
            """,
            (list(EXPECTED_TABLES),),
        ).fetchone()
        return int(rows[0]) if rows else 0

    def database_info(self) -> dict[str, object]:
        info = super().database_info()
        info["sqlite_path"] = ""
        return info

    def allocate_session(self, user: UserToken, local_port: int, client_info: str = "") -> tuple[dict | None, str]:
        session_id = secrets.token_urlsafe(16)
        proxy_name = f"nekotunnel-{session_id[:8]}"
        with self.connect() as conn:
            active_count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id = ? AND status = 'active'",
                (user.id,),
            ).fetchone()[0]
            if active_count >= user.max_sessions:
                conn.rollback()
                return None, "max_sessions_reached"
            slot = conn.execute(
                """
                SELECT * FROM slots
                WHERE status = 'free'
                  AND tcp_status = 'ready'
                  AND COALESCE(server_addr, '') != ''
                  AND COALESCE(server_port, '') != ''
                  AND COALESCE(frp_token_hash_or_encrypted, '') != ''
                  AND COALESCE(frp_token_hash_or_encrypted, '') NOT LIKE '%...%'
                ORDER BY id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            ).fetchone()
            if not slot:
                conn.rollback()
                return None, "no_free_slot"
            conn.execute(
                """
                INSERT INTO sessions (id, user_id, slot_id, status, client_info, proxy_name)
                VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (session_id, user.id, slot["id"], client_info[:500], proxy_name),
            )
            conn.execute(
                """
                UPDATE slots
                SET status = 'busy', current_session_id = ?, error = '', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'free'
                """,
                (session_id, slot["id"]),
            )
            conn.commit()
        self.add_log(
            "user",
            "connect",
            f"User {user.name} opened session {session_id} on slot {slot['id']} ({slot['project_name']}/{slot['service_name']}) for local port {local_port}",
        )
        return {
            "session_id": session_id,
            "slot_id": slot["id"],
            "server_addr": slot["server_addr"],
            "server_port": int(slot["server_port"]),
            "frp_token": slot["frp_token_hash_or_encrypted"],
            "remote_port": int(slot["remote_port"] or 6000),
            "proxy_name": proxy_name,
        }, ""


def create_store():
    if database_type() == "postgres":
        return PostgresStore(database_url())
    return SQLiteStore()


def mask_token(token: str) -> str:
    if len(token) <= 10:
        return f"{token[:4]}..."
    return f"{token[:6]}...{token[-4:]}"


def token_prefix(token: str) -> str:
    return mask_token(token)


def account_label(accounts: list[RailwayAccount], account_id: int | None) -> str:
    if account_id is None:
        return "manual"
    account = next((item for item in accounts if item.id == account_id), None)
    return account.label if account else "unknown"


store = create_store()
