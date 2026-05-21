from dataclasses import dataclass


@dataclass
class RailwayAccount:
    id: int
    label: str
    workspace_override: str | None
    token_encrypted_or_masked: str
    token_prefix: str
    status: str
    error: str
    created_at: str
    updated_at: str

    @property
    def workspace(self) -> str:
        return self.workspace_override or "default"

    @property
    def token(self) -> str:
        return self.token_encrypted_or_masked


@dataclass
class RailwayProject:
    id: int
    railway_account_id: int | None
    project_name: str
    project_id: str | None
    environment_id: str | None
    workdir: str
    status: str
    error: str
    created_at: str
    updated_at: str

    @property
    def account_id(self) -> int | None:
        return self.railway_account_id


@dataclass
class Slot:
    id: int
    railway_account_id: int | None
    project_name: str
    service_name: str
    server_addr: str
    server_port: str
    frp_token_encrypted_or_masked: str
    frp_token_prefix: str
    remote_port: int
    status: str
    error: str
    created_at: str
    updated_at: str
    workdir: str | None = None
    frp_token_hash_or_encrypted: str | None = None
    deploy_status: str | None = None
    deployment_id: str | None = None
    last_deployed_at: str | None = None
    tcp_status: str | None = None
    tcp_last_checked_at: str | None = None
    project_id: str | None = None
    environment_id: str | None = None
    service_id: str | None = None
    service_instance_id: str | None = None
    railway_project_id: int | None = None
    current_session_id: str | None = None
    current_user_id: int | None = None
    current_user_name: str | None = None

    @property
    def account_id(self) -> int | None:
        return self.railway_account_id

    @property
    def server_address(self) -> str:
        return self.server_addr

    @property
    def tcp_address(self) -> str:
        if self.server_addr and self.server_port:
            return f"{self.server_addr}:{self.server_port}"
        return ""


@dataclass
class UserToken:
    id: int
    name: str
    token_hash: str
    token_prefix: str
    status: str
    max_sessions: int
    created_at: str
    updated_at: str


@dataclass
class TunnelSession:
    id: str
    user_id: int | None
    slot_id: int | None
    status: str
    started_at: str
    last_heartbeat_at: str
    ended_at: str | None
    client_info: str | None = None
    proxy_name: str | None = None
    user_name: str | None = None
    slot_label: str | None = None

    @property
    def user(self) -> str:
        return self.user_name or str(self.user_id or "-")

    @property
    def slot(self) -> str:
        return self.slot_label or str(self.slot_id or "-")

    @property
    def last_heartbeat(self) -> str:
        return self.last_heartbeat_at


@dataclass
class AuditLog:
    id: int
    actor: str
    action: str
    details: str
    created_at: str


@dataclass
class ProvisionLog:
    id: int
    railway_account_id: int | None
    action: str
    project_name: str
    status: str
    command: str
    stdout: str
    stderr: str
    error: str
    duration_ms: int
    created_at: str
    slot_id: int | None = None
    account_label: str = ""
    service_name: str = ""

    @property
    def account_id(self) -> int | None:
        return self.railway_account_id
