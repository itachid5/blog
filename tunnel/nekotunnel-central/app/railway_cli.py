import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import settings
from .storage import mask_token


MISSING_CLI_ERROR = "Railway CLI is not installed. Install with: bash <(curl -fsSL railway.com/install.sh) -y"
RAILWAY_TOKEN_REJECTED_ERROR = "Railway rejected this token. Copy the full token using Railway's copy button and add it again."
RAILWAY_GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"
logger = logging.getLogger("nekotunnel-central.railway")


@dataclass
class RailwayCliDiagnostics:
    detected_path: str
    version: str
    env_path: str
    cwd: str
    path_exists: bool
    env_token_present: bool
    env_token_masked: str
    account_token_source: str
    railway_home_dir: str
    railway_home_exists: bool
    railway_home_writable: bool
    persistent_disk_warning: str


@dataclass
class RailwayCommandResult:
    status: str
    command: str
    stdout: str
    stderr: str
    error: str
    duration_ms: int
    workdir: Path | None = None
    error_code: str = ""


def _is_executable(path: str) -> bool:
    return Path(path).is_file() and os.access(path, os.X_OK)


def detect_railway_cli() -> str:
    if settings.railway_cli_path:
        return settings.railway_cli_path
    render_cli = "/usr/local/bin/railway"
    if _is_executable(render_cli):
        return render_cli
    return shutil.which("railway") or "railway"


def railway_cli_diagnostics() -> RailwayCliDiagnostics:
    detected_path = detect_railway_cli()
    path_exists = _is_executable(detected_path)
    if not path_exists:
        version = MISSING_CLI_ERROR
    else:
        try:
            result = subprocess.run(
                [detected_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            version = (result.stdout or result.stderr).strip() or f"exited with {result.returncode}"
        except subprocess.TimeoutExpired:
            version = "version check timed out"
        except OSError as exc:
            version = str(exc)
    env_token = os.getenv("RAILWAY_API_TOKEN", "").strip()
    home_dir = Path(settings.railway_home_dir)
    home_exists = home_dir.exists()
    home_writable = home_exists and os.access(home_dir, os.W_OK)
    warning = ""
    if not home_exists or not home_writable or (settings.render and str(home_dir) != "/var/data/railway"):
        warning = "CLI login session may be lost after Render restart/redeploy unless a persistent disk is mounted at /var/data."
    return RailwayCliDiagnostics(
        detected_path=detected_path,
        version=version,
        env_path=settings.railway_cli_path or "",
        cwd=str(Path.cwd()),
        path_exists=path_exists,
        env_token_present=bool(env_token),
        env_token_masked=mask_token(env_token) if env_token else "",
        account_token_source="database",
        railway_home_dir=str(home_dir),
        railway_home_exists=home_exists,
        railway_home_writable=home_writable,
        persistent_disk_warning=warning,
    )


def safe_project_name(project_name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", project_name.strip()).strip("-._")
    return safe or "railway-project"


def mask_sensitive(text: str, token: str) -> str:
    if not text:
        return ""
    if not token:
        return text
    return text.replace(token, mask_token(token))


def _account_auth_type(auth) -> str:
    return getattr(auth, "auth_type", "token") or "token"


def _account_token(auth) -> str:
    if isinstance(auth, str):
        return auth
    return getattr(auth, "token_encrypted_or_masked", "") or ""


def _railway_env(auth) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("RAILWAY_TOKEN", None)
    if _account_auth_type(auth) == "cli_session":
        env.pop("RAILWAY_API_TOKEN", None)
        env["HOME"] = settings.railway_home_dir
    else:
        env["RAILWAY_API_TOKEN"] = _account_token(auth)
    return env


def _auth_mode_label(auth) -> str:
    if _account_auth_type(auth) == "cli_session":
        return f"CLI session HOME={settings.railway_home_dir}, Railway token env unset"
    return "RAILWAY_API_TOKEN env, RAILWAY_TOKEN unset"


def classify_railway_error(stdout: str, stderr: str, fallback: str) -> str:
    combined = f"{stdout}\n{stderr}\n{fallback}".lower()
    if "unauthorized" in combined or "invalid token" in combined or "forbidden" in combined:
        return "token_rejected"
    if "unexpected argument" in combined or "unknown command" in combined or "usage:" in combined:
        return "invalid_cli_command"
    if "workspace" in combined and ("not found" in combined or "could not find" in combined):
        return "workspace_not_found"
    return "railway_cli_failed"


def railway_error_message(stdout: str, stderr: str, fallback: str) -> str:
    error_code = classify_railway_error(stdout, stderr, fallback)
    if error_code == "token_rejected":
        return RAILWAY_TOKEN_REJECTED_ERROR
    return fallback


def command_for_log(cli_path: str, auth, args: list[str]) -> str:
    if _account_auth_type(auth) == "cli_session":
        parts = ["HOME=" + settings.railway_home_dir, "env", "-u", "RAILWAY_TOKEN", "-u", "RAILWAY_API_TOKEN", cli_path, *args]
    else:
        token = _account_token(auth)
        parts = ["env", "-u", "RAILWAY_TOKEN", f"RAILWAY_API_TOKEN={mask_token(token)}", cli_path, *args]
    return " ".join(shlex.quote(part) for part in parts)


def _railway_debug_context(auth, command: str, workdir: Path | None) -> dict[str, str]:
    token = _account_token(auth)
    return {
        "workdir": str(workdir or Path.cwd()),
        "auth_mode": _auth_mode_label(auth),
        "masked_token": mask_token(token) if token else "",
        "command": command,
    }


def run_railway_command(auth, args: list[str], workdir: Path | None = None, timeout: int = 90) -> RailwayCommandResult:
    cli_path = detect_railway_cli()
    token = _account_token(auth)
    logged_command = command_for_log(cli_path, auth, args)
    debug_context = _railway_debug_context(auth, logged_command, workdir)
    logger.info(
        "railway command start workdir=%s auth_mode=%s token=%s command=%s",
        debug_context["workdir"],
        debug_context["auth_mode"],
        debug_context["masked_token"],
        debug_context["command"],
    )
    if not _is_executable(cli_path):
        logger.error(
            "railway command end workdir=%s auth_mode=%s token=%s command=%s stdout=%r stderr=%r exit_code=%s duration_ms=%s",
            debug_context["workdir"],
            debug_context["auth_mode"],
            debug_context["masked_token"],
            debug_context["command"],
            "",
            MISSING_CLI_ERROR,
            "missing_cli",
            0,
        )
        return RailwayCommandResult("failed", logged_command, "", "", MISSING_CLI_ERROR, 0, workdir, "railway_cli_missing")

    if _account_auth_type(auth) == "cli_session":
        Path(settings.railway_home_dir).mkdir(parents=True, exist_ok=True)
    env = _railway_env(auth)

    started = time.monotonic()
    try:
        result = subprocess.run(
            [cli_path, *args],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = mask_sensitive(result.stdout, token)
        stderr = mask_sensitive(result.stderr, token)
        logger.info(
            "railway command end workdir=%s auth_mode=%s token=%s command=%s stdout=%r stderr=%r exit_code=%s duration_ms=%s",
            debug_context["workdir"],
            debug_context["auth_mode"],
            debug_context["masked_token"],
            debug_context["command"],
            stdout,
            stderr,
            result.returncode,
            duration_ms,
        )
        status = "success" if result.returncode == 0 else "failed"
        fallback = stderr or stdout or f"railway exited with {result.returncode}"
        error_code = "" if status == "success" else classify_railway_error(stdout, stderr, fallback)
        error = "" if status == "success" else railway_error_message(stdout, stderr, fallback)
        return RailwayCommandResult(status, logged_command, stdout, stderr, error, duration_ms, workdir, error_code)
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = mask_sensitive(exc.stdout or "", token)
        stderr = mask_sensitive(exc.stderr or "", token)
        logger.error(
            "railway command timeout workdir=%s auth_mode=%s token=%s command=%s stdout=%r stderr=%r exit_code=%s duration_ms=%s",
            debug_context["workdir"],
            debug_context["auth_mode"],
            debug_context["masked_token"],
            debug_context["command"],
            stdout,
            stderr,
            "timeout",
            duration_ms,
        )
        return RailwayCommandResult("failed", logged_command, stdout, stderr, f"Railway command timed out after {timeout} seconds.", duration_ms, workdir, "railway_cli_failed")
    except OSError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        error = mask_sensitive(str(exc), token)
        logger.error(
            "railway command error workdir=%s auth_mode=%s token=%s command=%s stdout=%r stderr=%r exit_code=%s duration_ms=%s",
            debug_context["workdir"],
            debug_context["auth_mode"],
            debug_context["masked_token"],
            debug_context["command"],
            "",
            error,
            "os_error",
            duration_ms,
        )
        return RailwayCommandResult("failed", logged_command, "", "", error, duration_ms, workdir, "railway_cli_failed")


def _parse_browserless_output(stdout: str, stderr: str) -> tuple[str | None, str | None]:
    combined = f"{stdout}\n{stderr}"
    url_match = re.search(r"https?://\S+", combined)
    login_url = url_match.group(0).rstrip(".,)") if url_match else None
    code = None
    code_patterns = (
        r"(?:code|pairing code)[:\s]+([A-Z0-9-]{4,})",
        r"enter\s+([A-Z0-9-]{4,})",
    )
    for pattern in code_patterns:
        match = re.search(pattern, combined, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip().strip(".")
            if not candidate.lower().startswith("http"):
                code = candidate
                break
    return login_url, code


def start_browserless_login(attempt_id: int, update_attempt: Callable[..., bool]) -> RailwayCommandResult:
    cli_path = detect_railway_cli()
    logged_command = command_for_log(cli_path, type("CliSessionAuth", (), {"auth_type": "cli_session"})(), ["login", "--browserless"])
    if not _is_executable(cli_path):
        update_attempt(attempt_id, status="failed", stderr=MISSING_CLI_ERROR, error=MISSING_CLI_ERROR, completed=True)
        return RailwayCommandResult("failed", logged_command, "", MISSING_CLI_ERROR, MISSING_CLI_ERROR, 0, None, "railway_cli_missing")

    Path(settings.railway_home_dir).mkdir(parents=True, exist_ok=True)
    env = _railway_env(type("CliSessionAuth", (), {"auth_type": "cli_session"})())
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            [cli_path, "login", "--browserless"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        error = str(exc)
        update_attempt(attempt_id, status="failed", stderr=error, error=error, completed=True)
        return RailwayCommandResult("failed", logged_command, "", error, error, 0, None, "railway_cli_failed")

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    output_lock = threading.Lock()

    def persist(status: str = "running", error: str | None = None, completed: bool = False) -> None:
        with output_lock:
            stdout = "".join(stdout_lines)
            stderr = "".join(stderr_lines)
        login_url, pairing_code = _parse_browserless_output(stdout, stderr)
        update_attempt(
            attempt_id,
            status=status,
            login_url=login_url,
            pairing_code=pairing_code,
            stdout=stdout[-8000:],
            stderr=stderr[-8000:],
            error=error,
            completed=completed,
        )

    def read_stream(stream, lines: list[str]) -> None:
        if stream is None:
            return
        for line in stream:
            with output_lock:
                lines.append(line)
            persist()

    def supervise() -> None:
        persist()
        threads = [
            threading.Thread(target=read_stream, args=(process.stdout, stdout_lines), daemon=True),
            threading.Thread(target=read_stream, args=(process.stderr, stderr_lines), daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            return_code = process.wait(timeout=600)
            for thread in threads:
                thread.join(timeout=1)
            if return_code == 0:
                persist(status="completed", completed=True)
            else:
                with output_lock:
                    fallback = "".join(stderr_lines) or "".join(stdout_lines) or f"railway exited with {return_code}"
                persist(status="failed", error=fallback[:500], completed=True)
        except subprocess.TimeoutExpired:
            process.kill()
            for thread in threads:
                thread.join(timeout=1)
            persist(status="failed", error="Railway browserless login timed out after 10 minutes.", completed=True)

    threading.Thread(target=supervise, daemon=True).start()
    duration_ms = int((time.monotonic() - started) * 1000)
    update_attempt(attempt_id, status="running")
    return RailwayCommandResult("success", logged_command, "", "", "", duration_ms)


def check_cli_session() -> RailwayCommandResult:
    auth = type("CliSessionAuth", (), {"auth_type": "cli_session"})()
    result = run_railway_command(auth, ["whoami"], timeout=30)
    if result.status == "success":
        return result
    return RailwayCommandResult(
        "failed",
        result.command,
        result.stdout,
        result.stderr,
        "Start Browserless Login first.",
        result.duration_ms,
        result.workdir,
        result.error_code or "cli_session_missing",
    )


def logout_cli_session() -> RailwayCommandResult:
    auth = type("CliSessionAuth", (), {"auth_type": "cli_session"})()
    return run_railway_command(auth, ["logout"], timeout=30)

def test_api_key(token: str) -> RailwayCommandResult:
    result = run_railway_command(token, ["whoami"], timeout=30)
    if result.status == "success":
        return result
    return RailwayCommandResult(
        "failed",
        result.command,
        result.stdout,
        result.stderr,
        "Railway whoami failed; this is diagnostic only and does not prove the token cannot create projects or services.",
        result.duration_ms,
        result.workdir,
        "diagnostic_failed",
    )


def graphql_me_test(token: str) -> RailwayCommandResult:
    command = f"POST {RAILWAY_GRAPHQL_URL} Authorization=Bearer {mask_token(token)} query='query {{ me {{ id name email }} }}'"
    started = time.monotonic()
    body = json.dumps({"query": "query { me { id name email } }"}).encode("utf-8")
    request = urllib.request.Request(
        RAILWAY_GRAPHQL_URL,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8", errors="replace")
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = mask_sensitive(response_body, token)
        try:
            payload = json.loads(response_body or "{}")
        except json.JSONDecodeError:
            return RailwayCommandResult("failed", command, stdout, "", "GraphQL me returned invalid JSON.", duration_ms, None, "railway_cli_failed")
        me = payload.get("data", {}).get("me") if isinstance(payload, dict) else None
        if isinstance(me, dict) and me.get("id"):
            return RailwayCommandResult("success", command, stdout, "", "", duration_ms)
        error_text = json.dumps(payload.get("errors", payload)) if isinstance(payload, dict) else "GraphQL me failed."
        error_code = "token_rejected" if "unauthorized" in error_text.lower() or "forbidden" in error_text.lower() else "railway_cli_failed"
        error = RAILWAY_TOKEN_REJECTED_ERROR if error_code == "token_rejected" else error_text
        return RailwayCommandResult("failed", command, stdout, mask_sensitive(error_text, token), error, duration_ms, None, error_code)
    except urllib.error.HTTPError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        detail = mask_sensitive(exc.read().decode("utf-8", errors="replace"), token)
        error_code = "token_rejected" if exc.code in {401, 403} or "unauthorized" in detail.lower() else "railway_cli_failed"
        error = RAILWAY_TOKEN_REJECTED_ERROR if error_code == "token_rejected" else detail or f"Railway GraphQL returned HTTP {exc.code}."
        return RailwayCommandResult("failed", command, "", detail, error, duration_ms, None, error_code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        error = mask_sensitive(str(exc), token)
        return RailwayCommandResult("failed", command, "", error, error, duration_ms, None, "railway_cli_failed")


def live_token_test(token: str) -> tuple[RailwayCommandResult, RailwayCommandResult | None]:
    cli_result = run_railway_command(token, ["whoami"], timeout=30)
    graphql_result = None if cli_result.status == "success" else graphql_me_test(token)
    return cli_result, graphql_result


def create_test_project(token: str) -> RailwayCommandResult:
    project_name = f"neko-token-live-test-{time.strftime('%Y%m%d%H%M%S')}"
    timestamp = time.strftime("%Y%m%d%H%M%S")
    workdir = Path("data") / "provision-work" / "projects" / f"{safe_project_name(project_name)}-{timestamp}"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "README.md").write_text(f"# {project_name}\n")
    args = ["init", "--name", project_name]
    result = run_railway_command(token, [*args, "--json"], workdir=workdir, timeout=90)
    if result.status == "success":
        return result
    return run_railway_command(token, args, workdir=workdir, timeout=90)


def create_project(project_name: str, token: str, workspace: str | None) -> RailwayCommandResult:
    timestamp = time.strftime("%Y%m%d%H%M%S")
    workdir = Path("data") / "provision-work" / "projects" / f"{safe_project_name(project_name)}-{timestamp}"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "README.md").write_text(f"# {project_name}\n")

    args = ["init", "--name", project_name]
    if workspace:
        args.extend(["--workspace", workspace])
    result = run_railway_command(token, [*args, "--json"], workdir=workdir, timeout=90)
    if result.status == "success":
        return result
    return run_railway_command(token, args, workdir=workdir, timeout=90)


def link_project(project_name: str, token: str, workdir: Path, workspace: str | None = None) -> RailwayCommandResult:
    args = ["link", "--project", project_name]
    if workspace:
        args.extend(["--workspace", workspace])
    args.append("--json")
    return run_railway_command(token, args, workdir=workdir, timeout=90)


def create_service(service_name: str, token: str, workdir: Path) -> RailwayCommandResult:
    return run_railway_command(token, ["add", "-s", service_name, "--json"], workdir=workdir, timeout=90)


def deploy_service(service_name: str, token: str, workdir: Path) -> RailwayCommandResult:
    result = run_railway_command(token, ["up", "-s", service_name, "--detach", "--json"], workdir=workdir, timeout=300)
    if result.status == "failed" and "json" in (result.stderr + result.stdout + result.error).lower():
        return run_railway_command(token, ["up", "-s", service_name, "--detach"], workdir=workdir, timeout=300)
    return result


def refresh_tcp_proxy(service_name: str, token: str, workdir: Path) -> RailwayCommandResult:
    return run_railway_command(
        token,
        ["run", "-s", service_name, "sh", "-c", 'printf "%s:%s" "$RAILWAY_TCP_PROXY_DOMAIN" "$RAILWAY_TCP_PROXY_PORT"'],
        workdir=workdir,
        timeout=90,
    )


def railway_status(token: str, workdir: Path) -> RailwayCommandResult:
    return run_railway_command(token, ["status", "--json"], workdir=workdir, timeout=90)


def railway_environment_config(token: str, workdir: Path) -> RailwayCommandResult:
    return run_railway_command(token, ["environment", "config", "--json"], workdir=workdir, timeout=90)


def railway_environment_show(token: str, workdir: Path) -> RailwayCommandResult:
    return run_railway_command(token, ["environment", "show", "--json"], workdir=workdir, timeout=90)


def railway_help(token: str, args: list[str], workdir: Path) -> RailwayCommandResult:
    return run_railway_command(token, [*args, "--help"], workdir=workdir, timeout=90)


def railway_environment_edit_service_config(token: str, service_name: str, path: str, value: str, workdir: Path) -> RailwayCommandResult:
    return run_railway_command(
        token,
        ["environment", "edit", "--service-config", service_name, path, value],
        workdir=workdir,
        timeout=90,
    )


def deployment_id_from_output(stdout: str) -> str:
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            for key in ("deploymentId", "deployment_id", "id"):
                value = payload.get(key)
                if isinstance(value, str):
                    return value
    return ""
