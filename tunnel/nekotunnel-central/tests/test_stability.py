import threading
from pathlib import Path

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

    client["write_frpc_config"](
        config_path,
        {
            "server_addr": "example.com",
            "server_port": 7000,
            "frp_token": "secret",
            "proxy_name": "proxy-1",
            "remote_port": 6000,
        },
        3389,
    )

    config = config_path.read_text()
    assert "loginFailExit = false" in config
    assert "[transport]" in config
    assert 'protocol = "tcp"' in config
    assert "tls.enable = true" in config
    assert "tcpMux = true" in config
    assert "tcpMuxKeepaliveInterval = 30" in config
    assert "heartbeatInterval = 20" in config
    assert "heartbeatTimeout = 120" in config
    assert "dialServerKeepalive = 30" in config


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
        endpoint_id="endpoint-rdp",
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
    assert '[str(executable), "run-service", protocol, str(local_port)]' in linux
    assert 'LOG_DIR / f"{endpoint_key(protocol, local_port)}.log"' in linux
    assert "def run_service(args: list[str])" in linux
    assert "run-service" in windows
    assert 'New-ScheduledTaskAction -Execute "powershell.exe"' in windows
    assert "-WindowStyle Hidden" in windows
    assert "New-ScheduledTaskSettingsSet -Hidden" in windows
    assert "Unregister-ScheduledTask" in windows
