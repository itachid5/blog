import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from app.storage import SQLiteStore


def load_linux_client_namespace() -> dict:
    text = Path("client/nekotunnel").read_text()
    start = text.index("<<'PY'\n") + len("<<'PY'\n")
    end = text.rindex("\nPY\n")
    namespace = {"__name__": "nekotunnel_client_test"}
    exec(compile(text[start:end], "client/nekotunnel<embedded-python>", "exec"), namespace)
    return namespace


def make_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nekotunnel.db")
    store.init_db()
    return store


def make_ready_slot(store: SQLiteStore, service_name: str, port: int = 7000) -> None:
    slot = store.add_manual_slot("project", service_name, "example.com", str(port), f"frp-token-{service_name}")
    with store.connect() as conn:
        conn.execute(
            "UPDATE slots SET status = 'free', tcp_status = 'ready', remote_port = ? WHERE id = ?",
            (port, slot.id),
        )
        conn.commit()


def test_endpoint_id_does_not_include_token():
    client = load_linux_client_namespace()

    first = client["stable_endpoint_id"]("https://api.example", "token-one", "machine-1", "tcp", 3389)
    second = client["stable_endpoint_id"]("https://api.example", "token-two", "machine-1", "tcp", 3389)
    different_port = client["stable_endpoint_id"]("https://api.example", "token-one", "machine-1", "tcp", 22)

    assert first == second
    assert first != different_port


def test_frpc_config_uses_verified_v057_transport_keys(tmp_path):
    client = load_linux_client_namespace()
    config_path = tmp_path / "frpc.toml"
    allocation = {
        "server_addr": "example.com",
        "server_port": 7000,
        "frp_token": "secret",
        "proxy_name": "proxy-1",
        "remote_port": 6000,
    }

    client["write_frpc_config"](config_path, allocation, 3389)

    config = config_path.read_text()
    assert "loginFailExit" not in config
    assert "tcpMuxKeepaliveInterval" not in config
    assert "dialServerKeepalive" not in config
    assert "tls.serverName" not in config
    assert 'serverAddr = "example.com"' in config
    assert "serverPort = 7000" in config
    assert "[auth]" in config
    assert 'method = "token"' in config
    assert 'token = "secret"' in config
    assert "[transport]" in config
    assert 'protocol = "tcp"' in config
    assert "heartbeatInterval = 20" in config
    assert "heartbeatTimeout = 120" in config
    assert "tcpMux = false" in config
    assert "tls.enable = true" in config
    assert "tls.disableCustomTLSFirstByte = true" in config
    assert "[[proxies]]" in config
    assert 'name = "proxy-1"' in config
    assert 'type = "tcp"' in config
    assert 'localIP = "127.0.0.1"' in config
    assert "localPort = 3389" in config
    assert "remotePort = 6000" in config

    client["write_frpc_config"](config_path, allocation, 22)
    assert "tcpMux = true" in config_path.read_text()

    client["write_frpc_config"](config_path, {**allocation, "tcp_mux": True}, 3389)
    assert "tcpMux = true" in config_path.read_text()


def test_frpc_verify_passes_when_binary_is_available(tmp_path):
    frpc = shutil.which("frpc")
    if frpc is None:
        pytest.skip("frpc binary is not installed")

    client = load_linux_client_namespace()
    config_path = tmp_path / "frpc.toml"
    client["write_frpc_config"](
        config_path,
        {
            "server_addr": "example.com",
            "server_port": 7000,
            "frp_token": "secret",
            "proxy_name": "proxy-1",
            "remote_port": 6000,
            "tcp_mux": True,
        },
        22,
    )

    result = subprocess.run([frpc, "verify", "-c", str(config_path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_heartbeat_failures_do_not_stop_service_loop(monkeypatch, capsys):
    client = load_linux_client_namespace()
    calls = {"waits": 0, "posts": 0}

    class StopEvent:
        def wait(self, _interval):
            calls["waits"] += 1
            return calls["waits"] > 2

    def failing_post(*_args, **_kwargs):
        calls["posts"] += 1
        raise RuntimeError("temporary outage")

    monkeypatch.setitem(client, "post_json", failing_post)

    client["heartbeat_loop"]("https://api.example", "token", "session", 1, StopEvent())

    assert calls["posts"] == 2
    assert "API heartbeat failure 2" in capsys.readouterr().out


def test_stale_cleanup_does_not_free_slot_before_threshold(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 1)
    make_ready_slot(store, "slot-1", 7001)

    allocation, error = store.allocate_session(
        user,
        3389,
        protocol="tcp",
        client_id="machine-a",
        endpoint_id="endpoint-tcp-3389",
        stale_seconds=600,
        grace_seconds=300,
    )

    assert error == ""
    assert allocation is not None
    assert store.expire_stale_sessions(600, 300) == []
    slot = store.get_slot(allocation["slot_id"])
    assert slot.status == "busy"
    assert slot.current_session_id == allocation["session_id"]


def test_clients_have_detached_service_entrypoints():
    linux = Path("client/nekotunnel").read_text()
    windows = Path("client/nekotunnel.ps1").read_text()

    assert "ExecStart={executable} run-service {protocol} {local_port}" in linux
    assert '[str(executable), "run-service", protocol, str(local_port), "--tcp-mux", str(tcp_mux).lower()]' in linux
    assert 'LOG_DIR / f"{endpoint_key(protocol, local_port)}.log"' in linux
    assert 'LOG_DIR / f"{endpoint_key(protocol, local_port)}.frpc.{stream}.log"' in linux
    assert "def run_service(args: list[str])" in linux
    assert "last_100_frpc_" in linux
    assert "--frpc|--report" in linux
    assert "logs <all|tcp <local_port>>" in linux
    assert "run-service" in windows
    assert 'New-ScheduledTaskAction -Execute "powershell.exe"' in windows
    assert "-WindowStyle Hidden" in windows
    assert 'loginFailExit' not in linux
    assert 'loginFailExit' not in windows
    assert 'tls.disableCustomTLSFirstByte = true' in linux
    assert 'tls.disableCustomTLSFirstByte = true' in windows
    assert 'tls.serverName' not in linux
    assert 'tls.serverName' not in windows
    assert "-RedirectStandardOutput $FrpcOut -RedirectStandardError $FrpcErr" in windows
    assert "frpc_exit_at=" in windows
    assert "last_100_frpc_out" in windows
    assert "--frpc|--report" in windows
    assert "logs <all|tcp <local_port>>" in windows
    assert "New-ScheduledTaskSettingsSet -Hidden" in windows
    assert "Unregister-ScheduledTask" in windows
