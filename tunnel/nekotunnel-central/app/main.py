import asyncio
import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import is_logged_in, login_admin, logout_admin, require_admin
from .config import settings
from .railway_cli import (
    create_project,
    create_service,
    deploy_service,
    deployment_id_from_output,
    link_project,
    railway_cli_diagnostics,
    railway_environment_config,
    railway_environment_edit_service_config,
    railway_environment_show,
    railway_help,
    railway_status,
    refresh_tcp_proxy,
    safe_project_name,
    test_api_key,
)
from .storage import account_label, mask_token, store

app = FastAPI(title=settings.app_name)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    max_age=settings.session_ttl_seconds,
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["mask_token"] = mask_token
templates.env.globals["account_label"] = account_label
templates.env.globals["settings"] = settings
logger = logging.getLogger("nekotunnel-central")


async def cleanup_expired_sessions() -> None:
    while True:
        await asyncio.sleep(settings.tunnel_cleanup_interval_seconds)
        store.expire_stale_sessions(settings.tunnel_session_ttl_seconds)


@app.on_event("startup")
def startup() -> None:
    store.init_db()
    db_info = store.database_info()
    logger.info("database=%s migration=%s tables=%s/%s", db_info["type"], db_info["migration_status"], db_info["table_count"], db_info["expected_table_count"])
    asyncio.create_task(cleanup_expired_sessions())


def flash(request: Request, category: str, message: str) -> None:
    request.session.setdefault("flashes", []).append({"category": category, "message": message})


def render(request: Request, template: str, context: dict | None = None):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    flashes = request.session.pop("flashes", [])
    return templates.TemplateResponse(
        request,
        template,
        {"flashes": flashes, "current_path": request.url.path, **(context or {})},
    )


@app.get("/login")
def login_page(request: Request):
    if is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    flashes = request.session.pop("flashes", [])
    return templates.TemplateResponse(request, "login.html", {"flashes": flashes, "current_path": "/login"})


@app.post("/login")
def login_submit(request: Request, admin_token: Annotated[str, Form()]):
    if secrets.compare_digest(admin_token, settings.admin_token):
        login_admin(request)
        flash(request, "success", "Logged in successfully.")
        return RedirectResponse("/", status_code=303)
    flash(request, "danger", "Invalid admin token.")
    return RedirectResponse("/login", status_code=303)


@app.post("/logout")
def logout(request: Request):
    logout_admin(request)
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def dashboard(request: Request):
    return render(request, "dashboard.html", {"stats": store.stats()})


@app.get("/railway-accounts")
def railway_accounts(request: Request):
    return render(request, "railway_accounts.html", {"accounts": store.railway_accounts})


@app.post("/railway-accounts")
def add_railway_account(
    request: Request,
    label: Annotated[str, Form()],
    railway_token: Annotated[str, Form()],
    workspace: Annotated[str, Form()] = "",
):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    store.add_railway_account(label.strip(), railway_token.strip(), workspace.strip() or None)
    flash(request, "success", "Railway account added. Token is saved for local Railway CLI tests and displayed only masked.")
    return RedirectResponse("/railway-accounts", status_code=303)


@app.post("/railway-accounts/{account_id}/check")
def check_railway_account(request: Request, account_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect

    account = store.get_account(account_id)
    if not account:
        flash(request, "danger", "Railway account not found.")
        return RedirectResponse("/railway-accounts", status_code=303)
    if account.status == "disabled":
        flash(request, "danger", "Railway account is disabled.")
        return RedirectResponse("/railway-accounts", status_code=303)
    token = account.token_encrypted_or_masked
    if not token or "..." in token:
        error = "Saved Railway token is masked and cannot be used for API key testing. Re-add the account token and try again."
        store.add_provision_log(account.id, "railway_api_key_test", account.label, "failed", "railway whoami", "", "", error, 0)
        flash(request, "danger", error)
        return RedirectResponse("/railway-accounts", status_code=303)

    result = test_api_key(token)
    store.add_provision_log(
        account.id,
        "railway_api_key_test",
        account.label,
        result.status,
        result.command,
        result.stdout,
        result.stderr,
        result.error,
        result.duration_ms,
    )
    if result.status == "success":
        flash(request, "success", f"Railway API key test succeeded for {account.label}.")
    else:
        flash(request, "danger", result.error or "Railway API key test failed.")
    return RedirectResponse("/railway-accounts", status_code=303)


@app.post("/railway-accounts/{account_id}/disable")
def disable_railway_account(request: Request, account_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    ok = store.disable_account(account_id)
    flash(request, "success" if ok else "danger", "Account disabled." if ok else "Account not found.")
    return RedirectResponse("/railway-accounts", status_code=303)


@app.post("/railway-accounts/{account_id}/delete")
def delete_railway_account(request: Request, account_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    ok = store.delete_account(account_id)
    flash(request, "success" if ok else "danger", "Account deleted." if ok else "Account not found.")
    return RedirectResponse("/railway-accounts", status_code=303)


DOCKERFILE_CONTENT = """FROM alpine:latest

RUN apk add --no-cache sslh wget tar ca-certificates

ENV FRP_VERSION=0.57.0

RUN wget https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_amd64.tar.gz && \\
    tar xzf frp_${FRP_VERSION}_linux_amd64.tar.gz && \\
    mv frp_${FRP_VERSION}_linux_amd64 /frp && \\
    rm frp_${FRP_VERSION}_linux_amd64.tar.gz

COPY frps.toml /frp/frps.toml
COPY start.sh /start.sh

RUN chmod +x /start.sh

CMD [\"/start.sh\"]
"""

START_SH_CONTENT = """#!/bin/sh
set -eu

FRPS_PID=""
SSLH_PID=""
STOPPING=0

log() {
    echo "[nekotunnel] $*"
}

stop_all() {
    STOPPING=1
    log "Stopping services..."

    if [ -n "$SSLH_PID" ]; then
        kill -TERM "$SSLH_PID" 2>/dev/null || true
    fi

    if [ -n "$FRPS_PID" ]; then
        kill -TERM "$FRPS_PID" 2>/dev/null || true
    fi

    wait "$SSLH_PID" 2>/dev/null || true
    wait "$FRPS_PID" 2>/dev/null || true

    log "Stopped."
}

trap 'stop_all; exit 0' INT TERM

run_frps() {
    while [ "$STOPPING" -eq 0 ]; do
        log "Starting frps..."
        /frp/frps -c /frp/frps.toml &
        FRPS_PID=$!
        wait "$FRPS_PID" || true

        if [ "$STOPPING" -eq 0 ]; then
            log "frps exited unexpectedly. Restarting in 2 seconds..."
            sleep 2
        fi
    done
}

run_sslh() {
    while [ "$STOPPING" -eq 0 ]; do
        if sslh -h 2>&1 | grep -q -- '--tls'; then
            TLS_OPT="--tls"
        else
            TLS_OPT="--ssl"
        fi

        log "Starting sslh on port ${PORT:-8080}..."

        sslh -f -u root \
          -p "0.0.0.0:${PORT:-8080}" \
          "$TLS_OPT" "127.0.0.1:7000" \
          --anyprot "127.0.0.1:6000" \
          --timeout 2 &
        SSLH_PID=$!

        wait "$SSLH_PID" || true

        if [ "$STOPPING" -eq 0 ]; then
            log "sslh exited unexpectedly. Restarting in 2 seconds..."
            sleep 2
        fi
    done
}

run_frps &
FRPS_SUPERVISOR_PID=$!

run_sslh &
SSLH_SUPERVISOR_PID=$!

wait "$FRPS_SUPERVISOR_PID" "$SSLH_SUPERVISOR_PID"

"""


def frps_toml(token: str) -> str:
    return f'''bindAddr = "127.0.0.1"
bindPort = 7000

proxyBindAddr = "127.0.0.1"

auth.method = "token"
auth.token = "{token}"

[transport]
heartbeatTimeout = 90
tcpMux = true
tcpMuxKeepaliveInterval = 30
tcpKeepalive = 7200
tls.force = true

[[allowPorts]]
single = 6000
'''


TCP_PROXY_PENDING_MESSAGE = "TCP Proxy is not enabled yet. Enable TCP Proxy manually on Railway service with internal port 8080, then refresh again."
TCP_AUTO_ENABLE_MANUAL_MESSAGE = "Automatic TCP Proxy enable is not available through documented Railway CLI/API in this build. Enable TCP Proxy manually with internal port 8080, then click Refresh TCP."
TCP_ENABLE_PENDING_MESSAGE = "TCP enable command succeeded, but Railway TCP variables are not available yet. Try Refresh TCP again."
TCP_CONFIG_CANDIDATES = [
    ("networking.tcpProxy.applicationPort", "8080"),
    ("networking.tcp.applicationPort", "8080"),
    ("tcpProxy.applicationPort", "8080"),
    ("tcpProxy.port", "8080"),
    ("networking.tcpProxy.enabled", "true"),
]
GRAPHQL_URL = "https://backboard.railway.app/graphql/v2"
GRAPHQL_TERMS = ("tcp", "proxy", "domain", "serviceinstance", "public", "networking")


def short_error(message: str) -> str:
    return " ".join((message or "Unknown deploy error").split())[:300]


def parse_tcp_proxy(stdout: str) -> tuple[str, int] | None:
    value = stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9.-]+:[0-9]+", value):
        return None
    domain, port_text = value.rsplit(":", 1)
    if not domain or any(not part for part in domain.split(".")):
        return None
    port = int(port_text)
    if port < 1 or port > 65535:
        return None
    return domain, port


def first_string_by_key(value, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in keys and isinstance(item, str):
                return item
        for item in value.values():
            found = first_string_by_key(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = first_string_by_key(item, keys)
            if found:
                return found
    return ""


def parse_railway_status_ids(stdout: str) -> dict[str, str]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    return {
        "project_id": first_string_by_key(payload, {"projectid", "project_id"}),
        "environment_id": first_string_by_key(payload, {"environmentid", "environment_id"}),
        "service_id": first_string_by_key(payload, {"serviceid", "service_id"}),
        "service_instance_id": first_string_by_key(payload, {"serviceinstanceid", "service_instance_id"}),
    }


def parse_project_create_ids(stdout: str) -> dict[str, str]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    project_id = payload.get("id") or payload.get("projectId") or payload.get("project_id") or ""
    environment_id = first_string_by_key(payload, {"environmentid", "environment_id"})
    return {
        "project_id": project_id if isinstance(project_id, str) else "",
        "environment_id": environment_id,
    }


def matching_graphql_names(value) -> list[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and any(term in name.lower() for term in GRAPHQL_TERMS):
            names.add(name)
        for item in value.values():
            names.update(matching_graphql_names(item))
    elif isinstance(value, list):
        for item in value:
            names.update(matching_graphql_names(item))
    return sorted(names)


def discover_graphql_tcp_mutations(token: str) -> tuple[str, str, str]:
    payload = json.dumps({"query": "query { __schema { mutationType { fields { name args { name type { name kind ofType { name kind } } } } } types { name } } }"}).encode()
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        return "failed", "", short_error(str(exc))
    duration = int((time.monotonic() - started) * 1000)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return "failed", body[:4000], "GraphQL introspection returned invalid JSON."
    names = matching_graphql_names(parsed.get("data", {}).get("__schema", {}).get("mutationType", {}).get("fields", []))
    if not names:
        names = matching_graphql_names(parsed.get("data", {}).get("__schema", {}).get("types", []))
    output = json.dumps({"matches": names, "duration_ms": duration}, indent=2)
    return ("found" if names else "success"), output, ""


def run_tcp_refresh(slot, account, token: str, action: str, pending_message: str = TCP_PROXY_PENDING_MESSAGE) -> tuple[str, str]:
    service_name = (slot.service_name or "final").strip() or "final"
    result = refresh_tcp_proxy(service_name, token, slot_workdir(slot))
    if result.status == "success":
        parsed = parse_tcp_proxy(result.stdout)
        if parsed:
            server_addr, server_port = parsed
            store.mark_slot_tcp_ready(slot.id, server_addr, server_port)
            store.add_provision_log(account.id, action, slot.project_name, "success", result.command, result.stdout, result.stderr, "", result.duration_ms, slot.id, account.label, service_name)
            return "success", f"TCP proxy ready: {server_addr}:{server_port}"
        store.mark_slot_tcp_pending(slot.id, pending_message)
        store.add_provision_log(account.id, action, slot.project_name, "pending", result.command, result.stdout, result.stderr, pending_message, result.duration_ms, slot.id, account.label, service_name)
        return "warning", pending_message
    error = short_error(result.error or result.stderr or result.stdout)
    store.mark_slot_tcp_failed(slot.id, error)
    store.add_provision_log(account.id, action, slot.project_name, "failed", result.command, result.stdout, result.stderr, error, result.duration_ms, slot.id, account.label, service_name)
    return "danger", error


def stored_project_id(project_name: str) -> str:
    stdout = store.latest_successful_project_stdout(project_name)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return ""
    value = payload.get("id") if isinstance(payload, dict) else ""
    return value if isinstance(value, str) else ""


def slot_workdir(slot) -> Path:
    if slot.railway_project_id:
        project = store.get_railway_project(slot.railway_project_id)
        if project and project.workdir:
            return Path(project.workdir)
    if slot.workdir:
        return Path(slot.workdir)
    workdir = Path("data") / "provision-work" / safe_project_name(slot.project_name)
    store.set_slot_workdir(slot.id, str(workdir))
    return workdir


def existing_slot_workdir(slot) -> Path | None:
    if slot.railway_project_id:
        project = store.get_railway_project(slot.railway_project_id)
        if project and project.workdir:
            return Path(project.workdir)
    if slot.workdir:
        return Path(slot.workdir)
    return None


def slot_account(slot):
    if slot.railway_project_id:
        project = store.get_railway_project(slot.railway_project_id)
        if project and project.railway_account_id:
            return store.get_account(project.railway_account_id)
    if slot.railway_account_id:
        return store.get_account(slot.railway_account_id)
    return None


def can_redeploy_slot(slot) -> bool:
    return bool((slot.railway_project_id or slot.workdir) and (slot.service_name or "").strip() and slot_account(slot))


def write_server_files(slot, account, token: str, service_name: str, workdir: Path | None = None):
    started = time.monotonic()
    workdir = workdir or slot_workdir(slot)
    command = "write Dockerfile frps.toml start.sh"
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        frp_token = slot.frp_token_hash_or_encrypted or f"frp_{secrets.token_urlsafe(32)}"
        if not slot.frp_token_hash_or_encrypted:
            store.set_slot_frp_token(slot.id, frp_token)
        (workdir / "Dockerfile").write_text(DOCKERFILE_CONTENT)
        (workdir / "frps.toml").write_text(frps_toml(frp_token))
        start_sh = workdir / "start.sh"
        start_sh.write_text(START_SH_CONTENT)
        start_sh.chmod(0o755)
        duration_ms = int((time.monotonic() - started) * 1000)
        store.add_provision_log(
            account.id,
            "write_files",
            slot.project_name,
            "success",
            command,
            f"Wrote server files in {workdir}",
            "",
            "",
            duration_ms,
            slot.id,
            account.label,
            service_name,
        )
        return True, "", workdir
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        error = short_error(str(exc))
        store.add_provision_log(
            account.id,
            "write_files",
            slot.project_name,
            "failed",
            command,
            "",
            "",
            error,
            duration_ms,
            slot.id,
            account.label,
            service_name,
        )
        return False, error, workdir


@app.get("/projects")
def projects(request: Request):
    projects_list = store.projects
    return render(
        request,
        "projects.html",
        {
            "accounts": store.railway_accounts,
            "projects": projects_list,
            "service_counts": store.project_service_counts(),
            "next_service_names": {project.id: store.next_service_name(project.id) for project in projects_list},
        },
    )


@app.post("/projects")
def create_railway_project(
    request: Request,
    account_id: Annotated[int, Form()],
    project_name: Annotated[str, Form()],
    workspace: Annotated[str, Form()] = "",
):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect

    account = store.get_account(account_id)
    clean_project_name = project_name.strip()
    if not account:
        flash(request, "danger", "Railway account not found.")
        return RedirectResponse("/projects", status_code=303)
    if account.status == "disabled":
        flash(request, "danger", "Railway account is disabled.")
        return RedirectResponse("/projects", status_code=303)
    token = account.token_encrypted_or_masked
    if not token or "..." in token:
        error = "Saved Railway token is masked and cannot be used for project creation. Re-add the account token and try again."
        store.add_provision_log(account.id, "create_project", clean_project_name, "failed", "railway init", "", "", error, 0, None, account.label)
        flash(request, "danger", error)
        return RedirectResponse("/projects", status_code=303)

    workspace_override = workspace.strip() or account.workspace_override or None
    result = create_project(clean_project_name, token, workspace_override)
    ids = parse_project_create_ids(result.stdout) if result.status == "success" else {}
    project = store.add_railway_project(
        account.id,
        clean_project_name,
        str(result.workdir) if result.workdir else "",
        "project_created" if result.status == "success" else "failed",
        result.error,
        ids.get("project_id", ""),
        ids.get("environment_id", ""),
    )
    store.add_provision_log(
        account.id,
        "create_project",
        clean_project_name,
        result.status,
        result.command,
        result.stdout,
        result.stderr,
        result.error,
        result.duration_ms,
        None,
        account.label,
    )
    if result.status == "success":
        flash(request, "success", f"Railway project created: {clean_project_name}")
    else:
        flash(request, "danger", result.error or "Railway project creation failed.")
    return RedirectResponse("/projects", status_code=303)


@app.post("/projects/{project_id}/refresh")
def refresh_project_status(request: Request, project_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    project = store.get_railway_project(project_id)
    if not project:
        flash(request, "danger", "Project not found.")
        return RedirectResponse("/projects", status_code=303)
    account = store.get_account(project.railway_account_id) if project.railway_account_id else None
    if not account or account.status == "disabled":
        flash(request, "danger", "Linked Railway account is missing or disabled.")
        return RedirectResponse("/projects", status_code=303)
    token = account.token_encrypted_or_masked
    if not token or "..." in token:
        error = "Saved Railway token is masked and cannot be used for project status refresh. Re-add the account token and try again."
        store.add_provision_log(account.id, "create_project", project.project_name, "failed", "railway status --json", "", "", error, 0, None, account.label)
        flash(request, "danger", error)
        return RedirectResponse("/projects", status_code=303)
    result = railway_status(token, Path(project.workdir))
    store.add_provision_log(account.id, "create_project", project.project_name, result.status, result.command, result.stdout, result.stderr, result.error, result.duration_ms, None, account.label)
    if result.status == "success":
        ids = parse_railway_status_ids(result.stdout)
        store.update_railway_project_ids(project.id, ids.get("project_id", ""), ids.get("environment_id", ""))
        store.update_railway_project_status(project.id, "project_created", "")
        flash(request, "success", "Project status refreshed.")
    else:
        store.update_railway_project_status(project.id, "failed", short_error(result.error))
        flash(request, "danger", result.error or "Project status refresh failed.")
    return RedirectResponse("/projects", status_code=303)


@app.post("/projects/{project_id}/delete")
def delete_project_record(request: Request, project_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    ok = store.delete_railway_project(project_id)
    flash(request, "success" if ok else "danger", "Project local record deleted." if ok else "Project not found.")
    return RedirectResponse("/projects", status_code=303)


@app.post("/projects/{project_id}/services")
def create_project_service(request: Request, project_id: int, service_name: Annotated[str, Form()] = ""):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    project = store.get_railway_project(project_id)
    if not project:
        flash(request, "danger", "Project not found.")
        return RedirectResponse("/projects", status_code=303)
    account = store.get_account(project.railway_account_id) if project.railway_account_id else None
    if not account or account.status == "disabled":
        flash(request, "danger", "Linked Railway account is missing or disabled.")
        return RedirectResponse("/projects", status_code=303)
    token = account.token_encrypted_or_masked
    if not token or "..." in token:
        flash(request, "danger", "Saved Railway token is masked and cannot be used for service deployment. Re-add the account token and try again.")
        return RedirectResponse("/projects", status_code=303)

    clean_service_name = service_name.strip() or store.next_service_name(project.id)
    if store.service_name_exists(project.id, clean_service_name):
        flash(request, "danger", f"Service name already exists in this project: {clean_service_name}")
        return RedirectResponse("/projects", status_code=303)

    frp_token = f"frp_{secrets.token_urlsafe(32)}"
    slot = store.create_service_slot(project, clean_service_name, frp_token)
    workdir = Path(project.workdir)
    ok, error, workdir = write_server_files(slot, account, token, clean_service_name, workdir)
    if not ok:
        store.mark_slot_deploy_failed(slot.id, error)
        flash(request, "danger", error)
        return RedirectResponse("/projects", status_code=303)

    create_result = create_service(clean_service_name, token, workdir)
    store.add_provision_log(account.id, "create_service", project.project_name, create_result.status, create_result.command, create_result.stdout, create_result.stderr, create_result.error, create_result.duration_ms, slot.id, account.label, clean_service_name)
    if create_result.status == "failed":
        error = short_error(create_result.error or create_result.stderr or create_result.stdout)
        store.mark_slot_deploy_failed(slot.id, error)
        flash(request, "danger", error)
        return RedirectResponse("/projects", status_code=303)

    deploy_result = deploy_service(clean_service_name, token, workdir)
    store.add_provision_log(account.id, "deploy_service", project.project_name, deploy_result.status, deploy_result.command, deploy_result.stdout, deploy_result.stderr, deploy_result.error, deploy_result.duration_ms, slot.id, account.label, clean_service_name)
    if deploy_result.status == "success":
        store.mark_slot_deployed(slot.id, clean_service_name, deployment_id_from_output(deploy_result.stdout))
        flash(request, "success", f"Created and deployed {project.project_name}/{clean_service_name}.")
    else:
        error = short_error(deploy_result.error or deploy_result.stderr or deploy_result.stdout)
        store.mark_slot_deploy_failed(slot.id, error)
        flash(request, "danger", error)
    return RedirectResponse("/projects", status_code=303)


@app.get("/slots")
def slots(request: Request, status: Annotated[str, Query()] = "all", project_id: Annotated[int | None, Query()] = None):
    selected_status = status if status in {"all", "free", "busy", "deployed", "tcp_pending", "failed"} else "all"
    slots_list = store.list_slots(None if selected_status == "all" else selected_status, project_id)
    projects_list = store.projects
    redeployable_slot_ids = {slot.id for slot in slots_list if can_redeploy_slot(slot)}
    return render(
        request,
        "slots.html",
        {
            "accounts": store.railway_accounts,
            "projects": projects_list,
            "slots": slots_list,
            "redeployable_slot_ids": redeployable_slot_ids,
            "selected_status": selected_status,
            "selected_project_id": project_id,
        },
    )


@app.post("/slots/test-create-project")
def test_create_railway_project(request: Request):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    flash(request, "warning", "Project creation moved to the Projects page.")
    return RedirectResponse("/projects", status_code=303)


@app.post("/slots/create")
def create_slot(
    request: Request,
    account_id: Annotated[int, Form()],
    project_name: Annotated[str, Form()],
    service_name: Annotated[str, Form()] = "final",
):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    store.add_placeholder_slot(account_id, project_name.strip(), service_name.strip() or "final")
    flash(request, "warning", "Provisioning not implemented yet. Placeholder slot created with tcp_pending status.")
    return RedirectResponse("/slots", status_code=303)


@app.post("/slots/{slot_id}/deploy")
def create_service_and_deploy(request: Request, slot_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect

    slot = store.get_slot(slot_id)
    if not slot:
        flash(request, "danger", "Slot not found.")
        return RedirectResponse("/slots", status_code=303)
    if slot.status not in {"project_created", "deploy_failed", "deployed"}:
        flash(request, "danger", "Only project_created, deploy_failed, or deployed slots can be deployed.")
        return RedirectResponse("/slots", status_code=303)
    account = slot_account(slot)
    if not account or account.status == "disabled":
        flash(request, "danger", "Linked Railway account is missing or disabled.")
        return RedirectResponse("/slots", status_code=303)
    token = account.token_encrypted_or_masked
    if not token or "..." in token:
        error = "Saved Railway token is masked and cannot be used for deployment. Re-add the account token and try again."
        store.mark_slot_deploy_failed(slot.id, error)
        flash(request, "danger", error)
        return RedirectResponse("/slots", status_code=303)

    service_name = (slot.service_name or "final").strip() or "final"
    ok, error, workdir = write_server_files(slot, account, token, service_name)
    if not ok:
        store.mark_slot_deploy_failed(slot.id, error)
        flash(request, "danger", error)
        return RedirectResponse("/slots", status_code=303)

    create_result = create_service(service_name, token, workdir)
    create_status = create_result.status
    create_error = create_result.error
    if create_result.status == "failed" and "already" in (create_result.stdout + create_result.stderr + create_result.error).lower():
        create_status = "success"
        create_error = "Service already exists; continuing to deploy."
    store.add_provision_log(
        account.id,
        "create_service",
        slot.project_name,
        create_status,
        create_result.command,
        create_result.stdout,
        create_result.stderr,
        create_error if create_status == "failed" else create_error,
        create_result.duration_ms,
        slot.id,
        account.label,
        service_name,
    )
    if create_status == "failed":
        error = short_error(create_error)
        store.mark_slot_deploy_failed(slot.id, error)
        flash(request, "danger", error)
        return RedirectResponse("/slots", status_code=303)

    deploy_result = deploy_service(service_name, token, workdir)
    store.add_provision_log(
        account.id,
        "deploy_service",
        slot.project_name,
        deploy_result.status,
        deploy_result.command,
        deploy_result.stdout,
        deploy_result.stderr,
        deploy_result.error,
        deploy_result.duration_ms,
        slot.id,
        account.label,
        service_name,
    )
    if deploy_result.status == "success":
        store.mark_slot_deployed(slot.id, service_name, deployment_id_from_output(deploy_result.stdout))
        flash(request, "success", f"Created service and started Railway deploy for {slot.project_name}/{service_name}.")
    else:
        error = short_error(deploy_result.error)
        store.mark_slot_deploy_failed(slot.id, error)
        flash(request, "danger", error)
    return RedirectResponse("/slots", status_code=303)


@app.post("/slots/{slot_id}/redeploy")
def redeploy_service(request: Request, slot_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect

    slot = store.get_slot(slot_id)
    if not slot:
        flash(request, "danger", "Slot not found.")
        return RedirectResponse("/slots", status_code=303)
    account = slot_account(slot)
    service_name = (slot.service_name or "").strip()
    if not account or account.status == "disabled":
        error = "Linked Railway account is missing or disabled."
        store.mark_slot_deploy_failed(slot.id, error)
        store.add_provision_log(slot.railway_account_id, "redeploy_service", slot.project_name, "failed", "railway up", "", "", error, 0, slot.id, "", service_name)
        flash(request, "danger", error)
        return RedirectResponse("/slots", status_code=303)
    if not service_name:
        error = "Missing service name. Cannot redeploy."
        store.mark_slot_deploy_failed(slot.id, error)
        store.add_provision_log(account.id, "redeploy_service", slot.project_name, "failed", "railway up", "", "", error, 0, slot.id, account.label, service_name)
        flash(request, "danger", error)
        return RedirectResponse("/slots", status_code=303)
    workdir = existing_slot_workdir(slot)
    if workdir is None:
        error = "Missing project workdir. Cannot redeploy."
        store.mark_slot_deploy_failed(slot.id, error)
        store.add_provision_log(account.id, "write_files", slot.project_name, "failed", "write Dockerfile frps.toml start.sh", "", "", error, 0, slot.id, account.label, service_name)
        store.add_provision_log(account.id, "redeploy_service", slot.project_name, "failed", "railway up", "", "", error, 0, slot.id, account.label, service_name)
        flash(request, "danger", error)
        return RedirectResponse("/slots", status_code=303)
    token = account.token_encrypted_or_masked
    if not token or "..." in token:
        error = "Saved Railway token is masked and cannot be used for redeployment. Re-add the account token and try again."
        store.mark_slot_deploy_failed(slot.id, error)
        store.add_provision_log(account.id, "redeploy_service", slot.project_name, "failed", "railway up", "", "", error, 0, slot.id, account.label, service_name)
        flash(request, "danger", error)
        return RedirectResponse("/slots", status_code=303)

    ok, error, workdir = write_server_files(slot, account, token, service_name, workdir)
    if not ok:
        store.mark_slot_deploy_failed(slot.id, error)
        flash(request, "danger", error)
        return RedirectResponse("/slots", status_code=303)

    deploy_result = deploy_service(service_name, token, workdir)
    store.add_provision_log(
        account.id,
        "redeploy_service",
        slot.project_name,
        deploy_result.status,
        deploy_result.command,
        deploy_result.stdout,
        deploy_result.stderr,
        deploy_result.error,
        deploy_result.duration_ms,
        slot.id,
        account.label,
        service_name,
    )
    if deploy_result.status == "success":
        store.mark_slot_redeployed(slot.id, deployment_id_from_output(deploy_result.stdout))
        flash(request, "success", "Redeploy complete. Latest server supervisor is now deployed. If TCP is already enabled, click Refresh TCP.")
    else:
        error = short_error(deploy_result.error or deploy_result.stderr or deploy_result.stdout)
        store.mark_slot_deploy_failed(slot.id, error)
        flash(request, "danger", error)
    return RedirectResponse("/slots", status_code=303)


@app.post("/slots/manual")
def add_manual_slot(
    request: Request,
    project_name: Annotated[str, Form()],
    service_name: Annotated[str, Form()],
    server_address: Annotated[str, Form()],
    server_port: Annotated[str, Form()],
    frp_token: Annotated[str, Form()],
):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    store.add_manual_slot(project_name.strip(), service_name.strip(), server_address.strip(), server_port.strip(), frp_token.strip())
    flash(request, "success", "Manual slot added as free.")
    return RedirectResponse("/slots", status_code=303)


@app.post("/slots/{slot_id}/refresh-tcp")
def refresh_tcp(request: Request, slot_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect

    slot = store.get_slot(slot_id)
    if not slot:
        flash(request, "danger", "Slot not found.")
        return RedirectResponse("/slots", status_code=303)
    if slot.status not in {"deployed", "tcp_pending"}:
        flash(request, "danger", "Only deployed or tcp_pending slots can refresh TCP.")
        return RedirectResponse("/slots", status_code=303)

    account = slot_account(slot)
    if not account or account.status == "disabled":
        error = "Linked Railway account is missing or disabled."
        store.mark_slot_tcp_failed(slot.id, error)
        store.add_provision_log(slot.railway_account_id, "refresh_tcp", slot.project_name, "failed", "railway run", "", "", error, 0, slot.id, "", slot.service_name)
        flash(request, "danger", error)
        return RedirectResponse("/slots", status_code=303)
    token = account.token_encrypted_or_masked
    if not token or "..." in token:
        error = "Saved Railway token is masked and cannot be used for TCP refresh. Re-add the account token and try again."
        store.mark_slot_tcp_failed(slot.id, error)
        store.add_provision_log(account.id, "refresh_tcp", slot.project_name, "failed", "railway run", "", "", error, 0, slot.id, account.label, slot.service_name)
        flash(request, "danger", error)
        return RedirectResponse("/slots", status_code=303)
    workdir = slot_workdir(slot)
    if not workdir.exists():
        error = "Missing linked project directory. Cannot run railway run."
        store.mark_slot_tcp_failed(slot.id, error)
        store.add_provision_log(account.id, "refresh_tcp", slot.project_name, "failed", "railway run", "", "", error, 0, slot.id, account.label, slot.service_name)
        flash(request, "danger", error)
        return RedirectResponse("/slots", status_code=303)

    category, message = run_tcp_refresh(slot, account, token, "refresh_tcp")
    flash(request, category, message)
    return RedirectResponse("/slots", status_code=303)


@app.post("/slots/{slot_id}/auto-enable-tcp")
def auto_enable_tcp(request: Request, slot_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    flash(request, "warning", TCP_PROXY_PENDING_MESSAGE)
    return RedirectResponse("/slots", status_code=303)


@app.post("/slots/{slot_id}/force-free")
def force_free(request: Request, slot_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    ok = store.force_release_slot(slot_id)
    flash(request, "success" if ok else "danger", "Slot forced free." if ok else "Slot not found.")
    return RedirectResponse("/slots", status_code=303)


@app.post("/slots/{slot_id}/force-release")
def force_release(request: Request, slot_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    ok = store.force_release_slot(slot_id)
    flash(request, "success" if ok else "danger", "Slot released." if ok else "Slot not found.")
    return RedirectResponse("/slots", status_code=303)


@app.post("/slots/{slot_id}/mark-offline")
def mark_offline(request: Request, slot_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    ok = store.update_slot_status(slot_id, "offline")
    flash(request, "success" if ok else "danger", "Slot marked offline." if ok else "Slot not found.")
    return RedirectResponse("/slots", status_code=303)


@app.post("/slots/{slot_id}/delete")
def delete_slot(request: Request, slot_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    ok = store.delete_slot(slot_id)
    flash(request, "success" if ok else "danger", "Slot deleted." if ok else "Slot not found.")
    return RedirectResponse("/slots", status_code=303)


@app.get("/users")
def users(request: Request):
    return render(request, "users.html", {"users": store.users})


@app.post("/users")
def create_user(request: Request, name: Annotated[str, Form()], max_sessions: Annotated[int, Form()] = 1):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    _, token = store.create_user_token(name.strip(), max(1, max_sessions))
    flash(request, "success", f"User token created. Copy it now; it will not be shown again: {token}")
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/disable")
def disable_user(request: Request, user_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    ok = store.disable_user(user_id)
    flash(request, "success" if ok else "danger", "User token disabled." if ok else "User not found.")
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/delete")
def delete_user(request: Request, user_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    ok = store.delete_user(user_id)
    flash(request, "success" if ok else "danger", "User token deleted." if ok else "User not found.")
    return RedirectResponse("/users", status_code=303)


@app.get("/sessions")
def sessions(request: Request):
    return render(request, "sessions.html", {"sessions": store.sessions})


@app.post("/sessions/{session_id}/force-close")
def force_close_session(request: Request, session_id: str):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    ok = store.force_close_session(session_id)
    flash(request, "success" if ok else "warning", "Session force closed and slot released." if ok else "Session not found.")
    return RedirectResponse("/sessions", status_code=303)



@app.get("/provision-logs")
def provision_logs(request: Request, status: Annotated[str, Query()] = "all"):
    allowed_filters = {"all", "create_project", "create_service", "deploy_service", "redeploy_service", "refresh_tcp", "failed", "success"}
    selected_status = status if status in allowed_filters else "all"
    logs = store.list_provision_logs(None if selected_status == "all" else selected_status)
    return render(
        request,
        "provision_logs.html",
        {
            "provision_logs": logs,
            "accounts": store.railway_accounts,
            "selected_status": selected_status,
        },
    )


@app.post("/provision-logs/{log_id}/delete")
def delete_provision_log(request: Request, log_id: int):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    ok = store.delete_provision_log(log_id)
    flash(request, "success" if ok else "danger", "Provision log deleted." if ok else "Provision log not found.")
    return RedirectResponse("/provision-logs", status_code=303)

@app.get("/logs")
def logs(request: Request):
    return render(request, "logs.html", {"logs": store.logs})


@app.get("/settings")
def settings_page(request: Request):
    provision_work_dir = Path("data") / "provision-work"
    stats = store.stats()
    return render(
        request,
        "settings.html",
        {
            "database_info": store.database_info(),
            "railway_cli": railway_cli_diagnostics(),
            "provision_work_dir": provision_work_dir,
            "provision_work_dir_exists": provision_work_dir.exists(),
            "slot_counts": stats,
        },
    )


INSTALL_SCRIPT = r'''#!/bin/sh
set -eu

api_url="__API_URL__"
install_dir="$HOME/.local/bin"
target="$install_dir/nekotunnel"

install_help() {
  cat <<'HELP'
Missing dependencies. Install them with one of:
  apt: sudo apt update && sudo apt install -y curl tar gzip
  apk: sudo apk add --no-cache curl tar gzip
  yum: sudo yum install -y curl tar gzip
  pkg: sudo pkg install -y curl tar gzip
HELP
}

missing=""
if command -v curl >/dev/null 2>&1; then
  download() { curl -fsSL -H "bypass-tunnel-reminder: true" "$1"; }
elif command -v wget >/dev/null 2>&1; then
  download() { wget --header="bypass-tunnel-reminder: true" -qO- "$1"; }
else
  missing="$missing curl-or-wget"
fi
for dep in tar gzip; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    missing="$missing $dep"
  fi
done

if [ -n "$missing" ]; then
  echo "Missing required tools:$missing" >&2
  install_help >&2
  exit 1
fi

mkdir -p "$install_dir"
download "$api_url/client/nekotunnel" > "$target"
chmod +x "$target"

echo "Installed nekotunnel to $target"
case ":$PATH:" in
  *":$install_dir:"*) ;;
  *)
    echo "Add this to your PATH if needed: export PATH=\"$install_dir:\$PATH\""
    ;;
esac

if [ "$#" -gt 0 ]; then
  exec "$target" "$@"
fi

echo "You can now use: nekotunnel token USER_TOKEN --api $api_url"
'''

WINDOWS_INSTALL_SCRIPT = r'''$ErrorActionPreference = "Stop"
$ApiUrl = "__API_URL__"
$InstallDir = Join-Path $env:USERPROFILE ".nekotunnel"
$ClientPath = Join-Path $InstallDir "nekotunnel.ps1"
$ShimPath = Join-Path $InstallDir "nekotunnel.cmd"
$Headers = @{ "bypass-tunnel-reminder" = "true" }

if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir | Out-Null
}

Invoke-WebRequest -UseBasicParsing -Headers $Headers -Uri ($ApiUrl.TrimEnd('/') + "/client/nekotunnel.ps1") -OutFile $ClientPath
Invoke-WebRequest -UseBasicParsing -Headers $Headers -Uri ($ApiUrl.TrimEnd('/') + "/client/nekotunnel.cmd") -OutFile $ShimPath

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$PathEntries = @()
if (-not [string]::IsNullOrEmpty($UserPath)) {
    $PathEntries = $UserPath -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
}
if ($PathEntries -notcontains $InstallDir) {
    $NewUserPath = if ([string]::IsNullOrEmpty($UserPath)) { $InstallDir } else { $UserPath.TrimEnd(';') + ';' + $InstallDir }
    [Environment]::SetEnvironmentVariable("Path", $NewUserPath, "User")
}

$CurrentEntries = $env:Path -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
if ($CurrentEntries -notcontains $InstallDir) {
    $env:Path = $InstallDir + ';' + $env:Path
}

Write-Host "Installed nekotunnel to $InstallDir"
Write-Host "You can now use: nekotunnel token USER_TOKEN --api $ApiUrl"
'''

FRPC_VERSION = "0.57.0"
FRPC_ARCHIVES = {
    f"frp_{FRPC_VERSION}_linux_amd64.tar.gz": "application/gzip",
    f"frp_{FRPC_VERSION}_linux_arm64.tar.gz": "application/gzip",
    f"frp_{FRPC_VERSION}_windows_amd64.zip": "application/zip",
}


@app.get("/install.sh")
def install_script(request: Request):
    api_url = str(request.base_url).rstrip("/")
    return Response(INSTALL_SCRIPT.replace("__API_URL__", api_url), media_type="text/x-shellscript")


@app.get("/client/nekotunnel")
def client_script():
    return FileResponse("client/nekotunnel", media_type="text/x-python", filename="nekotunnel")


@app.get("/install.ps1")
def windows_install_script(request: Request):
    api_url = str(request.base_url).rstrip("/")
    return Response(WINDOWS_INSTALL_SCRIPT.replace("__API_URL__", api_url), media_type="text/plain")


@app.get("/client/nekotunnel.ps1")
def windows_client_script():
    return FileResponse("client/nekotunnel.ps1", media_type="text/plain", filename="nekotunnel.ps1")


@app.get("/client/nekotunnel.cmd")
def windows_client_shim():
    return FileResponse("client/nekotunnel.cmd", media_type="text/plain", filename="nekotunnel.cmd")


@app.get("/client/frpc/{archive_name}")
def client_frpc_archive(archive_name: str):
    if archive_name not in FRPC_ARCHIVES:
        raise HTTPException(status_code=404, detail="Unsupported frpc archive.")
    cache_dir = Path("data") / "client-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / archive_name
    if not archive_path.exists():
        url = f"https://github.com/fatedier/frp/releases/download/v{FRPC_VERSION}/{archive_name}"
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                archive_path.write_bytes(response.read())
        except (urllib.error.URLError, TimeoutError) as exc:
            raise HTTPException(status_code=502, detail=f"Could not cache frpc {FRPC_VERSION}: {short_error(str(exc))}") from exc
    return FileResponse(archive_path, media_type=FRPC_ARCHIVES[archive_name], filename=archive_name)


@app.get("/health")
def health():
    return {"ok": True, "service": "nekotunnel-central", "database": store.database_type}


def json_token(payload: dict) -> str:
    return str(payload.get("token") or payload.get("user_token") or "")


def api_user(payload: dict):
    token = json_token(payload)
    if not token:
        return None
    user = store.user_for_token(token)
    if not user or user.status != "active":
        return None
    return user


@app.post("/api/connect")
async def api_connect(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    user = api_user(payload)
    if not user:
        return JSONResponse({"ok": False, "error": "invalid_token"}, status_code=401)
    try:
        local_port = int(payload.get("local_port"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid_local_port"}, status_code=400)
    if local_port < 1 or local_port > 65535:
        return JSONResponse({"ok": False, "error": "invalid_local_port"}, status_code=400)

    allocation, error = store.allocate_session(user, local_port, str(payload.get("client_info") or ""))
    if error:
        status_code = 409 if error in {"no_free_slot", "max_sessions_reached"} else 400
        return JSONResponse({"ok": False, "error": error}, status_code=status_code)

    return {
        "ok": True,
        "session_id": allocation["session_id"],
        "slot_id": allocation["slot_id"],
        "server_addr": allocation["server_addr"],
        "server_port": allocation["server_port"],
        "frp_token": allocation["frp_token"],
        "remote_port": allocation["remote_port"],
        "proxy_name": allocation["proxy_name"],
        "heartbeat_interval": 15,
    }


@app.post("/api/heartbeat")
async def api_heartbeat(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    user = api_user(payload)
    if not user:
        return JSONResponse({"ok": False, "error": "invalid_token"}, status_code=401)
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return JSONResponse({"ok": False, "error": "missing_session_id"}, status_code=400)
    if not store.update_session_heartbeat(session_id, user.id):
        return JSONResponse({"ok": False, "error": "session_not_active"}, status_code=404)
    return {"ok": True}


@app.post("/api/disconnect")
async def api_disconnect(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    user = api_user(payload)
    if not user:
        return JSONResponse({"ok": False, "error": "invalid_token"}, status_code=401)
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return JSONResponse({"ok": False, "error": "missing_session_id"}, status_code=400)
    if not store.close_session(session_id, user.id, "closed", "user"):
        return JSONResponse({"ok": False, "error": "session_not_found"}, status_code=404)
    return {"ok": True}
