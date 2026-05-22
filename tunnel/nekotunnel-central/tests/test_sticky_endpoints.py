from pathlib import Path

from app.storage import SQLiteStore


def make_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nekotunnel.db")
    store.init_db()
    return store


def make_ready_slot(store: SQLiteStore, service_name: str, port: int) -> None:
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


def endpoint_row(store: SQLiteStore, endpoint_id: str):
    with store.connect() as conn:
        return conn.execute("SELECT * FROM tunnel_endpoints WHERE endpoint_id = ?", (endpoint_id,)).fetchone()


def test_same_endpoint_reconnect_within_grace_reuses_slot(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)
    make_ready_slot(store, "slot-2", 7002)

    first, first_error = store.allocate_session(
        user, 22, protocol="tcp", client_id="client-a", endpoint_id="endpoint-ssh"
    )
    assert first_error == ""

    assert store.close_session(first["session_id"], user.id, release=False, grace_seconds=300)
    held_slot = slot_row(store, first["slot_id"])
    assert held_slot["status"] == "busy"
    assert held_slot["current_session_id"] == first["session_id"]

    second, second_error = store.allocate_session(
        user, 22, protocol="tcp", client_id="client-a", endpoint_id="endpoint-ssh"
    )

    assert second_error == ""
    assert second["slot_id"] == first["slot_id"]
    assert second["session_id"] != first["session_id"]
    assert second["reconnect_count"] == 1


def test_same_endpoint_after_grace_reuses_preferred_slot_if_free(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)
    make_ready_slot(store, "slot-2", 7002)

    first, first_error = store.allocate_session(
        user, 22, protocol="tcp", client_id="client-a", endpoint_id="endpoint-ssh"
    )
    assert first_error == ""

    assert store.close_session(first["session_id"], user.id, status="stale", release=False, grace_seconds=-1)
    assert store.release_expired_endpoint_grace() == ["endpoint-ssh"]
    freed_slot = slot_row(store, first["slot_id"])
    assert freed_slot["status"] == "free"

    second, second_error = store.allocate_session(
        user, 22, protocol="tcp", client_id="client-a", endpoint_id="endpoint-ssh"
    )

    assert second_error == ""
    assert second["slot_id"] == first["slot_id"]


def test_same_token_different_sticky_ports_get_different_slots(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)
    make_ready_slot(store, "slot-2", 7002)

    first, first_error = store.allocate_session(
        user, 22, protocol="tcp", client_id="client-a", endpoint_id="endpoint-ssh"
    )
    second, second_error = store.allocate_session(
        user, 9000, protocol="tcp", client_id="client-a", endpoint_id="endpoint-web"
    )

    assert first_error == ""
    assert second_error == ""
    assert first["slot_id"] != second["slot_id"]


def test_different_endpoint_same_port_is_rejected_while_active(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)
    make_ready_slot(store, "slot-2", 7002)

    first, first_error = store.allocate_session(
        user, 22, protocol="tcp", client_id="client-a", endpoint_id="endpoint-ssh"
    )
    duplicate, duplicate_error = store.allocate_session(
        user, 22, protocol="tcp", client_id="client-b", endpoint_id="endpoint-ssh-other"
    )

    assert first_error == ""
    assert first is not None
    assert duplicate is None
    assert duplicate_error == "port_already_active"


def test_sticky_session_limit_counts_active_endpoints(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 1)
    make_ready_slot(store, "slot-1", 7001)
    make_ready_slot(store, "slot-2", 7002)

    first, first_error = store.allocate_session(
        user, 22, protocol="tcp", client_id="client-a", endpoint_id="endpoint-ssh"
    )
    second, second_error = store.allocate_session(
        user, 9000, protocol="tcp", client_id="client-a", endpoint_id="endpoint-web"
    )

    assert first_error == ""
    assert first is not None
    assert second is None
    assert second_error == "session_limit_reached"


def test_stale_sticky_session_enters_grace_without_freeing_slot(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)

    first, first_error = store.allocate_session(
        user, 22, protocol="tcp", client_id="client-a", endpoint_id="endpoint-ssh"
    )
    assert first_error == ""
    with store.connect() as conn:
        conn.execute(
            "UPDATE sessions SET last_heartbeat_at = '2000-01-01 00:00:00' WHERE id = ?",
            (first["session_id"],),
        )
        conn.commit()

    expired = store.expire_stale_sessions(ttl_seconds=1, grace_seconds=300)

    assert expired == [first["session_id"]]
    held_slot = slot_row(store, first["slot_id"])
    endpoint = endpoint_row(store, "endpoint-ssh")
    assert held_slot["status"] == "busy"
    assert held_slot["current_session_id"] == first["session_id"]
    assert endpoint["status"] == "reconnecting"
    assert endpoint["grace_until"] is not None



def test_sticky_reconnect_preserves_additive_profile_metadata(tmp_path):
    store = make_store(tmp_path)
    user, _token = store.create_user_token("alice", 3)
    make_ready_slot(store, "slot-1", 7001)

    first, first_error = store.allocate_session(
        user,
        3389,
        protocol="tcp",
        client_id="client-a",
        endpoint_id="endpoint-tcp-3389",
        tcp_mux=False,
        route_mode="mux",
        connection_profile="generic",
    )
    assert first_error == ""
    assert store.close_session(first["session_id"], user.id, release=False, grace_seconds=300)

    second, second_error = store.allocate_session(
        user,
        3389,
        protocol="tcp",
        client_id="client-a",
        endpoint_id="endpoint-tcp-3389",
        tcp_mux=False,
        route_mode="mux",
        connection_profile="generic",
    )

    assert second_error == ""
    assert second["slot_id"] == first["slot_id"]
    assert second["reconnect_count"] == 1
    assert second["tcp_mux"] is False
    assert second["route_mode"] == "mux"
    assert second["connection_profile"] == "generic"

def test_clients_expose_persistent_service_commands():
    linux = Path("client/nekotunnel").read_text()
    windows = Path("client/nekotunnel.ps1").read_text()

    assert "install-service" in linux
    assert "enable-boot" in linux
    assert "install-system-service" in linux
    assert "install-service" in windows
    assert "install-system-service" in windows
