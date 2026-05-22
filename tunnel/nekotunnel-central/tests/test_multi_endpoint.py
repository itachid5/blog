from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.storage import SQLiteStore


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


def test_same_token_allocates_different_ports_with_free_slots(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)
    make_ready_slot(store, "slot-2", 7002)

    first, first_error = store.allocate_session(user, 22, protocol="tcp")
    second, second_error = store.allocate_session(user, 9000, protocol="tcp")

    assert first_error == ""
    assert second_error == ""
    assert first["session_id"] != second["session_id"]
    assert first["slot_id"] != second["slot_id"]


def test_same_token_cannot_allocate_duplicate_active_port(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)
    make_ready_slot(store, "slot-2", 7002)

    first, first_error = store.allocate_session(user, 22, protocol="tcp")
    duplicate, duplicate_error = store.allocate_session(user, 22, protocol="tcp")

    assert first_error == ""
    assert first is not None
    assert duplicate is None
    assert duplicate_error == "port_already_active"


def test_max_active_sessions_rejects_third_endpoint(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 2)
    make_ready_slot(store, "slot-1", 7001)
    make_ready_slot(store, "slot-2", 7002)
    make_ready_slot(store, "slot-3", 7003)

    assert store.allocate_session(user, 22, protocol="tcp")[1] == ""
    assert store.allocate_session(user, 9000, protocol="tcp")[1] == ""
    third, third_error = store.allocate_session(user, 3000, protocol="tcp")

    assert third is None
    assert third_error == "session_limit_reached"


def test_one_slot_cannot_back_multiple_active_sessions(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)

    first, first_error = store.allocate_session(user, 22, protocol="tcp")
    second, second_error = store.allocate_session(user, 9000, protocol="tcp")

    assert first_error == ""
    assert first is not None
    assert second is None
    assert second_error == "no_available_slot"


def test_api_connect_maps_invalid_token(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    monkeypatch.setattr(main, "store", store)
    client = TestClient(main.app)

    response = client.post("/api/connect", json={"token": "bad", "protocol": "tcp", "local_port": 22})

    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "invalid_token"}


def test_api_connect_maps_no_available_slot(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    _user, token = store.create_user_token("alice", 3)
    monkeypatch.setattr(main, "store", store)
    client = TestClient(main.app)

    response = client.post("/api/connect", json={"token": token, "protocol": "tcp", "local_port": 22})

    assert response.status_code == 409
    assert response.json() == {"ok": False, "error": "no_available_slot"}


def test_api_connect_maps_session_limit_reached(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    _user, token = store.create_user_token("alice", 1)
    make_ready_slot(store, "slot-1", 7001)
    make_ready_slot(store, "slot-2", 7002)
    monkeypatch.setattr(main, "store", store)
    client = TestClient(main.app)

    first = client.post("/api/connect", json={"token": token, "protocol": "tcp", "local_port": 22})
    second = client.post("/api/connect", json={"token": token, "protocol": "tcp", "local_port": 9000})

    assert first.status_code == 200
    assert first.json()["ok"] is True
    assert second.status_code == 409
    assert second.json() == {"ok": False, "error": "session_limit_reached", "max_active_sessions": 1}


def test_clients_use_endpoint_specific_background_names():
    linux = Path("client/nekotunnel").read_text()
    windows = Path("client/nekotunnel.ps1").read_text()

    assert "nekotunnel-{endpoint_key(protocol, local_port)}.service" in linux
    assert "nekotunnel-{endpoint_key(protocol, local_port)}.pid" in linux
    assert 'LOG_DIR / f"{endpoint_key(protocol, local_port)}.log"' in linux
    assert "BACKGROUND_ENDPOINTS" in linux
    assert "NekoTunnel-$(Endpoint-Key $Protocol $Port)" in windows
    assert "background_endpoints" in windows
    assert "state-" in windows
    assert "logs" in windows
    assert "run-service" in windows
