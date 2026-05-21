import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .storage import mask_token


MISSING_CLI_ERROR = "Railway CLI is not installed. Install with: bash <(curl -fsSL railway.com/install.sh) -y"
RAILWAY_TOKEN_REJECTED_ERROR = "Railway rejected this token. Copy the full token using Railway's copy button and add it again. Make sure this is an Account Token from Railway Account → Tokens."


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


@dataclass
class RailwayCommandResult:
    status: str
    command: str
    stdout: str
    stderr: str
    error: str
    duration_ms: int
    workdir: Path | None = None


def _is_executable(path: str) -> bool:
    return Path(path).is_file() and os.access(path, os.X_OK)


def detect_railway_cli() -> str:
    if settings.railway_cli_path and _is_executable(settings.railway_cli_path):
        return settings.railway_cli_path
    home_cli = Path.home() / ".railway" / "bin" / "railway"
    if _is_executable(str(home_cli)):
        return str(home_cli)
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
    return RailwayCliDiagnostics(
        detected_path=detected_path,
        version=version,
        env_path=settings.railway_cli_path or "",
        cwd=str(Path.cwd()),
        path_exists=path_exists,
        env_token_present=bool(env_token),
        env_token_masked=mask_token(env_token) if env_token else "",
        account_token_source="database",
    )


def safe_project_name(project_name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", project_name.strip()).strip("-._")
    return safe or "railway-project"


def mask_sensitive(text: str, token: str) -> str:
    if not text:
        return ""
    return text.replace(token, mask_token(token))


def railway_error_message(stdout: str, stderr: str, fallback: str) -> str:
    combined = f"{stdout}\n{stderr}\n{fallback}".lower()
    if "unauthorized" in combined:
        return RAILWAY_TOKEN_REJECTED_ERROR
    return fallback


def command_for_log(cli_path: str, token: str, args: list[str]) -> str:
    parts = ["env", "-u", "RAILWAY_TOKEN", f"RAILWAY_API_TOKEN={mask_token(token)}", cli_path, *args]
    return " ".join(shlex.quote(part) for part in parts)


def run_railway_command(token: str, args: list[str], workdir: Path | None = None, timeout: int = 90) -> RailwayCommandResult:
    cli_path = detect_railway_cli()
    logged_command = command_for_log(cli_path, token, args)
    if not _is_executable(cli_path):
        return RailwayCommandResult("failed", logged_command, "", "", MISSING_CLI_ERROR, 0, workdir)

    env = os.environ.copy()
    env.pop("RAILWAY_TOKEN", None)
    env["RAILWAY_API_TOKEN"] = token

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
        status = "success" if result.returncode == 0 else "failed"
        error = "" if status == "success" else railway_error_message(stdout, stderr, stderr or stdout or f"railway exited with {result.returncode}")
        return RailwayCommandResult(status, logged_command, stdout, stderr, error, duration_ms, workdir)
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = mask_sensitive(exc.stdout or "", token)
        stderr = mask_sensitive(exc.stderr or "", token)
        return RailwayCommandResult("failed", logged_command, stdout, stderr, f"Railway command timed out after {timeout} seconds.", duration_ms, workdir)
    except OSError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return RailwayCommandResult("failed", logged_command, "", "", mask_sensitive(str(exc), token), duration_ms, workdir)


def test_api_key(token: str) -> RailwayCommandResult:
    result = run_railway_command(token, ["whoami"], timeout=30)
    if result.status == "success":
        return result
    return run_railway_command(token, ["projects", "--json"], timeout=30)


def create_project(project_name: str, token: str, workspace: str | None) -> RailwayCommandResult:
    timestamp = time.strftime("%Y%m%d%H%M%S")
    workdir = Path("data") / "provision-work" / "projects" / f"{safe_project_name(project_name)}-{timestamp}"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "README.md").write_text(f"# {project_name}\n")

    args = ["init", "--name", project_name]
    if workspace:
        args.extend(["--workspace", workspace])
    args.append("--json")
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
