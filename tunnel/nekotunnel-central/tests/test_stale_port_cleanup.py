from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.storage import SQLiteStore


def load_linux_client_namespace() -> dict:
    text = Path("client/nekotunnel").read_text()
    marker = "exec python3 - \"$@\" <<'PY'\n"
    start = text.index(marker) + len(marker)
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


def slot_row(store: SQLiteStore, slot_id: int):
    with store.connect() as conn:
        return conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()


def test_disconnect_port_releases_stale_session_without_endpoint(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)

    allocation, error = store.allocate_session(
        user, 22, protocol="tcp", client_id="client-a", endpoint_id="endpoint-ssh"
    )
    assert error == ""
    assert store.close_session(allocation["session_id"], user.id, status="stale", release=False, grace_seconds=300)

    result = store.disconnect_port(user.id, "tcp", 22)

    assert result == {"ok": True, "closed": 1, "released_slots": [allocation["slot_id"]]}
    assert slot_row(store, allocation["slot_id"])["status"] == "free"


def test_same_endpoint_stale_reconnect_succeeds(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)
    make_ready_slot(store, "slot-2", 7002)

    first, first_error = store.allocate_session(
        user, 22, protocol="tcp", client_id="client-a", endpoint_id="endpoint-ssh"
    )
    assert first_error == ""
    assert store.close_session(first["session_id"], user.id, status="stale", release=False, grace_seconds=300)

    second, second_error = store.allocate_session(
        user, 22, protocol="tcp", client_id="client-a", endpoint_id="endpoint-ssh"
    )

    assert second_error == ""
    assert second is not None
    assert second["slot_id"] == first["slot_id"]


def test_different_active_endpoint_still_returns_port_already_active(tmp_path):
    store = make_store(tmp_path)
    user, token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)
    make_ready_slot(store, "slot-2", 7002)
    monkeypatch_store = store
    client = TestClient(main.app)
    original_store = main.store
    main.store = monkeypatch_store
    try:
        first = client.post(
            "/api/connect",
            json={
                "token": token,
                "protocol": "tcp",
                "local_port": 22,
                "client_id": "client-a",
                "endpoint_id": "endpoint-active-111111",
            },
        )
        duplicate = client.post(
            "/api/connect",
            json={
                "token": token,
                "protocol": "tcp",
                "local_port": 22,
                "client_id": "client-b",
                "endpoint_id": "endpoint-active-222222",
            },
        )
    finally:
        main.store = original_store

    assert first.status_code == 200
    body = duplicate.json()
    assert duplicate.status_code == 409
    assert body["error"] == "port_already_active"
    assert body["local_port"] == 22
    assert body["blocker_type"] == "endpoint"
    assert body["blocker_id"]
    assert body["status"] == "active"
    assert body["endpoint_id_short"] == "endpoint-act"
    assert body["same_endpoint"] is False
    assert body["reason"] == "active_endpoint_same_port"
    assert body["suggested_command"] == "nekotunnel cleanup tcp 22 --force"


def test_api_disconnect_port_closes_same_endpoint_active_session(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    _user, token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)
    monkeypatch.setattr(main, "store", store)
    client = TestClient(main.app)
    connected = client.post(
        "/api/connect",
        json={
            "token": token,
            "protocol": "tcp",
            "local_port": 22,
            "client_id": "client-a",
            "endpoint_id": "endpoint-ssh",
        },
    ).json()

    response = client.post(
        "/api/disconnect-port",
        json={"token": token, "protocol": "tcp", "local_port": 22, "endpoint_id": "endpoint-ssh"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "closed": 1, "released_slots": [connected["slot_id"]]}
    assert slot_row(store, connected["slot_id"])["status"] == "free"


def test_stop_tcp_calls_server_cleanup(monkeypatch):
    client = load_linux_client_namespace()
    calls = []
    monkeypatch.setitem(client, "systemd_available", lambda: False)
    monkeypatch.setitem(client, "stop_nohup", lambda protocol, port: False)
    monkeypatch.setitem(client, "kill_frpc_for_endpoint", lambda protocol, port: 0)
    monkeypatch.setitem(client, "disconnect_saved_state", lambda path=client["STATE_PATH"]: True)
    monkeypatch.setitem(client, "remove_background_endpoint", lambda protocol, port: None)

    def cleanup(protocol, port, endpoint_id="", client_id=""):
        calls.append((protocol, port, endpoint_id, client_id))
        return True, {"ok": True, "closed": 1, "released_slots": [7]}, ""

    monkeypatch.setitem(client, "cleanup_server_port", cleanup)

    assert client["run_stop"](["tcp", "22"]) == 0
    assert calls == [("tcp", 22, "", "")]


def test_self_test_releases_allocated_session_on_verify_failure(tmp_path, monkeypatch):
    client = load_linux_client_namespace()
    client["APP_DIR"] = tmp_path
    disconnected = []
    monkeypatch.setitem(client, "load_config", lambda: {"API_URL": "https://api.example", "USER_TOKEN": "ntk_secret"})
    monkeypatch.setitem(client, "ensure_frpc", lambda api_url: tmp_path / "frpc")
    monkeypatch.setitem(client, "machine_id", lambda: "machine-a")

    def post_json(api_url, path, payload):
        assert path == "/api/connect"
        return 200, {
            "ok": True,
            "session_id": "session-123456",
            "server_addr": "example.com",
            "server_port": 7000,
            "frp_token": "secret",
            "remote_port": 6000,
            "proxy_name": "proxy-1",
            "tcp_mux": True,
            "route_mode": "mux",
        }, "{}"

    monkeypatch.setitem(client, "post_json", post_json)
    monkeypatch.setitem(client, "verify_frpc_config", lambda frpc, config: (False, "bad config"))
    monkeypatch.setitem(client, "disconnect", lambda api_url, token, session_id, release=True: disconnected.append((session_id, release)) or True)

    assert client["run_self_test"](["tcp", "22"]) == 1
    assert disconnected == [("session-123456", True)]



def test_stale_endpoint_reservation_cleanup_releases_slot(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)
    allocation, error = store.allocate_session(
        user, 22, protocol="tcp", client_id="client-a", endpoint_id="endpoint-stale"
    )
    assert error == ""
    with store.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (allocation["session_id"],))
        conn.execute(
            """
            UPDATE tunnel_endpoints
            SET status = 'active'
            WHERE endpoint_id = ?
            """,
            ("endpoint-stale",),
        )
        conn.commit()

    details = store.port_conflict_details(user.id, "tcp", 22, "endpoint-other")
    result = store.disconnect_port(user.id, "tcp", 22)

    assert details["reason"] == "stale_invalid_endpoint_reservation"
    assert result["closed"] == 0
    assert result["released_slots"] == [allocation["slot_id"]]
    assert slot_row(store, allocation["slot_id"])["status"] == "free"


def test_slot_current_session_id_missing_cleanup_releases_slot(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)
    allocation, error = store.allocate_session(
        user, 22, protocol="tcp", client_id="client-a", endpoint_id="endpoint-slot"
    )
    assert error == ""
    with store.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (allocation["session_id"],))
        conn.execute(
            """
            UPDATE tunnel_endpoints
            SET status = 'stale'
            WHERE endpoint_id = ?
            """,
            ("endpoint-slot",),
        )
        conn.commit()

    result = store.disconnect_port(user.id, "tcp", 22)

    assert result["released_slots"] == [allocation["slot_id"]]
    slot = slot_row(store, allocation["slot_id"])
    assert slot["status"] == "free"
    assert slot["current_session_id"] is None


def test_force_cleanup_closes_same_user_same_port_only(tmp_path):
    store = make_store(tmp_path)
    alice, _alice_token = store.create_user_token("alice", 3)
    bob, _bob_token = store.create_user_token("bob", 3)
    make_ready_slot(store, "slot-1", 7001)
    make_ready_slot(store, "slot-2", 7002)
    alice_allocation, alice_error = store.allocate_session(
        alice, 22, protocol="tcp", client_id="client-a", endpoint_id="endpoint-alice"
    )
    bob_allocation, bob_error = store.allocate_session(
        bob, 22, protocol="tcp", client_id="client-b", endpoint_id="endpoint-bob"
    )
    assert alice_error == ""
    assert bob_error == ""

    result = store.disconnect_port(alice.id, "tcp", 22, force=True)

    assert result["closed"] == 1
    assert result["released_slots"] == [alice_allocation["slot_id"]]
    assert slot_row(store, alice_allocation["slot_id"])["status"] == "free"
    assert slot_row(store, bob_allocation["slot_id"])["status"] == "busy"
    with store.connect() as conn:
        bob_session = conn.execute("SELECT status FROM sessions WHERE id = ?", (bob_allocation["session_id"],)).fetchone()
    assert bob_session["status"] == "active"


def test_cross_user_endpoint_id_collision_is_scoped_not_blocked(tmp_path):
    store = make_store(tmp_path)
    alice, _alice_token = store.create_user_token("alice", 3)
    bob, _bob_token = store.create_user_token("bob", 3)
    make_ready_slot(store, "slot-1", 7001)
    make_ready_slot(store, "slot-2", 7002)
    first, first_error = store.allocate_session(
        alice, 22, protocol="tcp", client_id="same-machine", endpoint_id="same-endpoint"
    )
    second, second_error = store.allocate_session(
        bob, 22, protocol="tcp", client_id="same-machine", endpoint_id="same-endpoint"
    )

    assert first_error == ""
    assert second_error == ""
    assert first["endpoint_id"] == "same-endpoint"
    assert second["endpoint_id"] != "same-endpoint"


def test_cleanup_force_works_from_linux_client(monkeypatch):
    client = load_linux_client_namespace()
    calls = []
    monkeypatch.setitem(client, "systemd_available", lambda: False)
    monkeypatch.setitem(client, "stop_nohup", lambda protocol, port: False)
    monkeypatch.setitem(client, "kill_frpc_for_endpoint", lambda protocol, port: 0)
    monkeypatch.setitem(client, "disconnect_saved_state", lambda path=client["STATE_PATH"]: True)
    monkeypatch.setitem(client, "remove_background_endpoint", lambda protocol, port: None)

    def cleanup(protocol, port, endpoint_id="", client_id="", force=False):
        calls.append((protocol, port, force))
        return True, {"ok": True, "closed": 2, "released_slots": [7]}, ""

    monkeypatch.setitem(client, "cleanup_server_port", cleanup)

    assert client["run_cleanup"](["tcp", "22", "--force"]) == 0
    assert calls == [("tcp", 22, True)]
