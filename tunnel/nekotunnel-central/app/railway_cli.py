import base64
import errno
import fnmatch
import hashlib
import io
import json
import logging
import os
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cryptography.fernet import Fernet, InvalidToken

from .config import settings
from .storage import mask_token, store


MISSING_CLI_ERROR = "Railway CLI is not installed. Install with: bash <(curl -fsSL railway.com/install.sh) -y"
RAILWAY_TOKEN_REJECTED_ERROR = "Railway rejected this token. Copy the full token using Railway's copy button and add it again."
RAILWAY_GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"
CLI_BACKUP_SECRET_ERROR = "Set a strong APP_SECRET before saving Railway CLI session backups."
CLI_BACKUP_TOO_LARGE_ERROR = "CLI session backup too large. Cache/log files may be included."
MAX_CLI_BACKUP_BYTES = 1024 * 1024
EXCLUDED_BACKUP_DIRS = {"cache", "logs", "tmp", "node_modules", "__pycache__"}
EXCLUDED_BACKUP_PATTERNS = ("*.log", "*.zip", "*.tar.gz", "*.gz", "*.exe")
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


@dataclass
class CliSessionBackupResult:
    status: str
    error: str = ""
    sha256: str = ""
    size_bytes: int = 0


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
    configured_home = Path(settings.railway_home_dir)
    try:
        configured_home.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    home_dir = Path(_effective_railway_home_dir())
    home_exists = home_dir.exists()
    home_writable = home_exists and os.access(home_dir, os.W_OK)
    warning = ""
    if str(home_dir) == "/tmp/railway":
        warning = "Login will be lost after restart."
    elif not home_exists or not home_writable or (settings.render and str(home_dir) != "/var/data/railway"):
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


def _effective_railway_home_dir() -> str:
    configured_home = Path(settings.railway_home_dir)
    try:
        configured_home.mkdir(parents=True, exist_ok=True)
        if os.access(configured_home, os.W_OK):
            return str(configured_home)
    except OSError:
        pass
    fallback_home = Path("/tmp/railway")
    fallback_home.mkdir(parents=True, exist_ok=True)
    return str(fallback_home)


def app_secret_strong_enough() -> bool:
    secret = settings.app_secret.strip()
    if not secret:
        return False
    lowered = secret.lower()
    weak_values = {"change-this-secret", "change-me", "dev-session-secret-change-me", "secret", "password"}
    return lowered not in weak_values and "change-this" not in lowered and len(secret) >= 32


def _fernet() -> Fernet:
    if not app_secret_strong_enough():
        raise ValueError(CLI_BACKUP_SECRET_ERROR)
    digest = hashlib.sha256(settings.app_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _excluded_backup_path(relative_path: Path) -> bool:
    parts = relative_path.parts
    if any(part in EXCLUDED_BACKUP_DIRS for part in parts):
        return True
    name = relative_path.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDED_BACKUP_PATTERNS)


def _safe_backup_relative_path(path: Path, root: Path) -> Path | None:
    try:
        relative_path = path.relative_to(root)
    except ValueError:
        return None
    if not relative_path.parts or path.is_symlink() or any(part in {"", ".", ".."} for part in relative_path.parts):
        return None
    return relative_path


def _create_cli_session_archive(railway_home_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(railway_home_dir.rglob("*")):
            relative_path = _safe_backup_relative_path(path, railway_home_dir)
            if relative_path is None or _excluded_backup_path(relative_path):
                if path.is_dir():
                    continue
                continue
            archive.add(path, arcname=str(relative_path), recursive=False)
            if buffer.tell() > MAX_CLI_BACKUP_BYTES:
                raise ValueError(CLI_BACKUP_TOO_LARGE_ERROR)
    data = buffer.getvalue()
    if len(data) > MAX_CLI_BACKUP_BYTES:
        raise ValueError(CLI_BACKUP_TOO_LARGE_ERROR)
    return data


def _safe_tar_member(member: tarfile.TarInfo) -> bool:
    path = Path(member.name)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts) and not member.issym() and not member.islnk()


def backup_cli_session(account_id: int) -> CliSessionBackupResult:
    account = store.get_account(account_id)
    if not account or account.auth_type != "cli_session":
        return CliSessionBackupResult("failed", "CLI session backup is only available for CLI-session accounts.")
    railway_home_dir = Path(_effective_railway_home_dir())
    if not railway_home_dir.exists():
        error = "Railway CLI session directory is missing."
        store.add_provision_log(account.id, "cli_session_backup", account.label, "failed", "backup railway home", "", "", error, 0, None, account.label)
        return CliSessionBackupResult("failed", error)
    try:
        plaintext = _create_cli_session_archive(railway_home_dir)
        digest = hashlib.sha256(plaintext).hexdigest()
        encrypted_blob = _fernet().encrypt(plaintext).decode("utf-8")
        store.save_cli_session_backup(account.id, encrypted_blob, digest, len(plaintext))
        store.add_provision_log(account.id, "cli_session_backup", account.label, "success", "backup railway home", "", "", "", 0, None, account.label)
        return CliSessionBackupResult("success", "", digest, len(plaintext))
    except (OSError, ValueError) as exc:
        error = str(exc)
    except Exception:
        logger.exception("CLI session backup failed for account %s", account_id)
        error = "CLI session backup failed."
    store.add_provision_log(account.id, "cli_session_backup", account.label, "failed", "backup railway home", "", "", error, 0, None, account.label)
    return CliSessionBackupResult("failed", error)


def _extract_cli_session_archive(data: bytes, railway_home_dir: Path) -> None:
    railway_home_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            if not _safe_tar_member(member):
                raise ValueError("CLI session backup contains an unsafe path.")
        for member in members:
            archive.extract(member, railway_home_dir)
    for path in railway_home_dir.rglob("*"):
        try:
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)
        except OSError:
            pass


def restore_cli_session(account_id: int) -> CliSessionBackupResult:
    account = store.get_account(account_id)
    if not account or account.auth_type != "cli_session":
        return CliSessionBackupResult("failed", "CLI session restore is only available for CLI-session accounts.")
    railway_home_dir = Path(_effective_railway_home_dir())
    if railway_home_dir.exists() and check_cli_session().status == "success":
        store.update_railway_account_status(account.id, "active", "")
        return CliSessionBackupResult("success")
    backup = store.latest_cli_session_backup(account.id)
    if not backup:
        error = "No encrypted CLI session backup found."
        store.update_railway_account_status(account.id, "failed", error)
        return CliSessionBackupResult("failed", error)
    try:
        plaintext = _fernet().decrypt(backup["encrypted_blob"].encode("utf-8"))
        digest = hashlib.sha256(plaintext).hexdigest()
        expected_digest = backup.get("sha256") or ""
        if expected_digest and digest != expected_digest:
            raise ValueError("CLI session backup integrity check failed.")
        _extract_cli_session_archive(plaintext, railway_home_dir)
        result = check_cli_session()
        if result.status == "success":
            store.mark_cli_session_backup_restored(account.id)
            store.update_railway_account_status(account.id, "active", "")
            store.add_provision_log(account.id, "cli_session_restore", account.label, "success", "restore railway home", "", "", "", 0, None, account.label)
            return CliSessionBackupResult("success", "", digest, len(plaintext))
        error = short_cli_error(result.error or result.stderr or result.stdout)
    except InvalidToken:
        error = "CLI session backup could not be decrypted. APP_SECRET may have changed."
    except (OSError, tarfile.TarError, ValueError) as exc:
        error = str(exc)
    except Exception:
        logger.exception("CLI session restore failed for account %s", account_id)
        error = "CLI session restore failed."
    store.mark_cli_session_backup_error(account.id, error)
    store.update_railway_account_status(account.id, "failed", short_cli_error(error))
    store.add_provision_log(account.id, "cli_session_restore", account.label, "failed", "restore railway home", "", "", short_cli_error(error), 0, None, account.label)
    return CliSessionBackupResult("failed", short_cli_error(error))


def short_cli_error(error: str) -> str:
    return (error or "").strip()[:500]


def _railway_env(auth) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("RAILWAY_TOKEN", None)
    if _account_auth_type(auth) == "cli_session":
        env.pop("RAILWAY_API_TOKEN", None)
        env.pop("CI", None)
        env["HOME"] = _effective_railway_home_dir()
        env["TERM"] = "xterm"
    else:
        env["RAILWAY_API_TOKEN"] = _account_token(auth)
    return env


def _auth_mode_label(auth) -> str:
    if _account_auth_type(auth) == "cli_session":
        return f"CLI session HOME={_effective_railway_home_dir()}, Railway token env unset"
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
        parts = ["HOME=" + _effective_railway_home_dir(), "env", "-u", "RAILWAY_TOKEN", "-u", "RAILWAY_API_TOKEN", cli_path, *args]
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
        Path(_effective_railway_home_dir()).mkdir(parents=True, exist_ok=True)
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


ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
DEVICE_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def _parse_browserless_output(stdout: str, stderr: str) -> tuple[str | None, str | None]:
    combined = strip_ansi(f"{stdout}\n{stderr}")
    railway_url_match = re.search(r"https://railway\.com/\S+", combined)
    url_match = railway_url_match or re.search(r"https?://\S+", combined)
    login_url = url_match.group(0).rstrip(".,);]") if url_match else None
    code_match = DEVICE_CODE_RE.search(combined.upper())
    return login_url, code_match.group(0) if code_match else None


def start_browserless_login(attempt_id: int, update_attempt: Callable[..., bool]) -> RailwayCommandResult:
    cli_path = detect_railway_cli()
    cli_auth = type("CliSessionAuth", (), {"auth_type": "cli_session"})()
    logged_command = command_for_log(cli_path, cli_auth, ["login", "--browserless"])
    if not _is_executable(cli_path):
        update_attempt(attempt_id, status="failed", stderr=MISSING_CLI_ERROR, error=MISSING_CLI_ERROR, completed=True)
        return RailwayCommandResult("failed", logged_command, "", MISSING_CLI_ERROR, MISSING_CLI_ERROR, 0, None, "railway_cli_missing")

    Path(_effective_railway_home_dir()).mkdir(parents=True, exist_ok=True)
    env = _railway_env(cli_auth)
    started = time.monotonic()
    try:
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            [cli_path, "login", "--browserless"],
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        os.close(slave_fd)
    except OSError as exc:
        error = str(exc)
        update_attempt(attempt_id, status="failed", stderr=error, error=error, completed=True)
        return RailwayCommandResult("failed", logged_command, "", error, error, 0, None, "railway_cli_failed")

    output_parts: list[str] = []
    output_lock = threading.Lock()

    def persist(status: str = "waiting", error: str | None = None, completed: bool = False) -> None:
        with output_lock:
            output = strip_ansi("".join(output_parts))
        login_url, pairing_code = _parse_browserless_output(output, "")
        update_attempt(
            attempt_id,
            status=status,
            login_url=login_url,
            pairing_code=pairing_code,
            stdout=output[-8000:],
            stderr="",
            error=error,
            completed=completed,
        )

    def terminate_process_group() -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def read_pty_until_exit() -> None:
        while True:
            readable, _, _ = select.select([master_fd], [], [], 0.5)
            if master_fd in readable:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                text = strip_ansi(chunk.decode("utf-8", errors="replace"))
                with output_lock:
                    output_parts.append(text)
                persist(status="waiting")
            if process.poll() is not None:
                readable, _, _ = select.select([master_fd], [], [], 0)
                if master_fd not in readable:
                    break

    def supervise() -> None:
        persist(status="started")
        try:
            reader = threading.Thread(target=read_pty_until_exit, daemon=True)
            reader.start()
            try:
                return_code = process.wait(timeout=600)
            except subprocess.TimeoutExpired:
                terminate_process_group()
                reader.join(timeout=1)
                persist(status="timeout", error="Railway browserless login timed out after 10 minutes.", completed=True)
                return
            reader.join(timeout=1)
            if return_code == 0:
                persist(status="completed", completed=True)
            else:
                with output_lock:
                    output = strip_ansi("".join(output_parts))
                fallback = output or f"railway exited with {return_code}"
                persist(status="failed", error=fallback[-500:], completed=True)
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass

    threading.Thread(target=supervise, daemon=True).start()
    duration_ms = int((time.monotonic() - started) * 1000)
    update_attempt(attempt_id, status="started")
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


def create_project(project_name: str, auth, workspace: str | None) -> RailwayCommandResult:
    timestamp = time.strftime("%Y%m%d%H%M%S")
    workdir = Path("data") / "provision-work" / "projects" / f"{safe_project_name(project_name)}-{timestamp}"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "README.md").write_text(f"# {project_name}\n")

    args = ["init", "--name", project_name]
    if workspace:
        args.extend(["--workspace", workspace])
    result = run_railway_command(auth, [*args, "--json"], workdir=workdir, timeout=90)
    if result.status == "success":
        return result
    return run_railway_command(auth, args, workdir=workdir, timeout=90)


def link_project(project_name: str, auth, workdir: Path, workspace: str | None = None) -> RailwayCommandResult:
    args = ["link", "--project", project_name]
    if workspace:
        args.extend(["--workspace", workspace])
    args.append("--json")
    return run_railway_command(auth, args, workdir=workdir, timeout=90)


def create_service(service_name: str, auth, workdir: Path) -> RailwayCommandResult:
    return run_railway_command(auth, ["add", "-s", service_name, "--json"], workdir=workdir, timeout=90)


def deploy_service(service_name: str, auth, workdir: Path) -> RailwayCommandResult:
    result = run_railway_command(auth, ["up", "-s", service_name, "--detach", "--json"], workdir=workdir, timeout=300)
    if result.status == "failed" and "json" in (result.stderr + result.stdout + result.error).lower():
        return run_railway_command(auth, ["up", "-s", service_name, "--detach"], workdir=workdir, timeout=300)
    return result


def refresh_tcp_proxy(service_name: str, auth, workdir: Path) -> RailwayCommandResult:
    return run_railway_command(
        auth,
        ["run", "-s", service_name, "sh", "-c", 'printf "%s:%s" "$RAILWAY_TCP_PROXY_DOMAIN" "$RAILWAY_TCP_PROXY_PORT"'],
        workdir=workdir,
        timeout=90,
    )


def railway_status(auth, workdir: Path) -> RailwayCommandResult:
    return run_railway_command(auth, ["status", "--json"], workdir=workdir, timeout=90)


def railway_environment_config(auth, workdir: Path) -> RailwayCommandResult:
    return run_railway_command(auth, ["environment", "config", "--json"], workdir=workdir, timeout=90)


def railway_environment_show(auth, workdir: Path) -> RailwayCommandResult:
    return run_railway_command(auth, ["environment", "show", "--json"], workdir=workdir, timeout=90)


def railway_help(auth, args: list[str], workdir: Path) -> RailwayCommandResult:
    return run_railway_command(auth, [*args, "--help"], workdir=workdir, timeout=90)


def railway_environment_edit_service_config(auth, service_name: str, path: str, value: str, workdir: Path) -> RailwayCommandResult:
    return run_railway_command(
        auth,
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
