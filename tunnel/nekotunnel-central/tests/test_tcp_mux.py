import asyncio
import contextlib
import socket
from dataclasses import dataclass

import pytest

from app.tcp_mux import CONTROL_MARKER, Target, handle_client


async def read_exact_prefix(reader: asyncio.StreamReader, prefix: bytes) -> bytes:
    data = await asyncio.wait_for(reader.read(len(prefix)), timeout=2)
    return data


async def start_target(label: bytes):
    seen: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        data = await reader.read(1024)
        seen.append(data)
        writer.write(label + data)
        await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, seen


def tls_client_hello(sni: str | None = None, alpn: list[str] | None = None) -> bytes:
    extensions = b""
    if sni is not None:
        name = sni.encode("ascii")
        server_name = b"\x00" + len(name).to_bytes(2, "big") + name
        server_name_list = len(server_name).to_bytes(2, "big") + server_name
        extensions += (0).to_bytes(2, "big") + len(server_name_list).to_bytes(2, "big") + server_name_list
    if alpn is not None:
        protocols = b"".join(len(proto.encode("ascii")).to_bytes(1, "big") + proto.encode("ascii") for proto in alpn)
        protocol_list = len(protocols).to_bytes(2, "big") + protocols
        extensions += (16).to_bytes(2, "big") + len(protocol_list).to_bytes(2, "big") + protocol_list
    body = (
        b"\x03\x03"
        + (b"\x00" * 32)
        + b"\x00"
        + (2).to_bytes(2, "big")
        + b"\x13\x01"
        + b"\x01\x00"
        + len(extensions).to_bytes(2, "big")
        + extensions
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x03" + len(handshake).to_bytes(2, "big") + handshake


@dataclass
class MuxFixture:
    server: asyncio.AbstractServer
    port: int
    control_seen: list[bytes]
    data_seen: list[bytes]


async def start_mux(data_port: int | None = None) -> MuxFixture:
    control_server, control_port, control_seen = await start_target(b"control:")
    data_seen: list[bytes] = []
    servers = [control_server]
    if data_port is None:
        data_server, data_port, data_seen = await start_target(b"data:")
        servers.append(data_server)

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await handle_client(
            reader,
            writer,
            Target("control", "127.0.0.1", control_port),
            Target("data", "127.0.0.1", data_port),
            1,
            0.01,
        )

    mux_server = await asyncio.start_server(handler, "127.0.0.1", 0)
    mux_port = mux_server.sockets[0].getsockname()[1]
    mux_server._nekotunnel_target_servers = servers
    return MuxFixture(mux_server, mux_port, control_seen, data_seen)


async def close_mux(fixture: MuxFixture) -> None:
    fixture.server.close()
    await fixture.server.wait_closed()
    for server in fixture.server._nekotunnel_target_servers:
        server.close()
        await server.wait_closed()


async def send_to_mux(port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(1024), timeout=2)
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return data


def unused_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.mark.asyncio
async def test_tls_without_control_marker_routes_to_data_port():
    mux = await start_mux()
    try:
        payload = tls_client_hello(sni="example.com")
        response = await send_to_mux(mux.port, payload)
        assert response == b"data:" + payload
        assert mux.data_seen == [payload]
        assert mux.control_seen == []
    finally:
        await close_mux(mux)


@pytest.mark.asyncio
async def test_tls_sni_control_marker_routes_to_control_port():
    mux = await start_mux()
    try:
        payload = tls_client_hello(sni=CONTROL_MARKER)
        response = await send_to_mux(mux.port, payload)
        assert response == b"control:" + payload
        assert mux.control_seen == [payload]
        assert mux.data_seen == []
    finally:
        await close_mux(mux)


@pytest.mark.asyncio
async def test_tls_alpn_control_marker_routes_to_control_port():
    mux = await start_mux()
    try:
        payload = tls_client_hello(alpn=[CONTROL_MARKER])
        response = await send_to_mux(mux.port, payload)
        assert response == b"control:" + payload
        assert mux.control_seen == [payload]
        assert mux.data_seen == []
    finally:
        await close_mux(mux)


@pytest.mark.asyncio
async def test_non_tls_first_bytes_route_to_data_port():
    mux = await start_mux()
    try:
        payload = b"TCP-data"
        response = await send_to_mux(mux.port, payload)
        assert response == b"data:" + payload
        assert mux.data_seen == [payload]
        assert mux.control_seen == []
    finally:
        await close_mux(mux)


@pytest.mark.asyncio
async def test_data_target_refused_does_not_stop_listener():
    mux = await start_mux(data_port=unused_port())
    try:
        response = await send_to_mux(mux.port, b"not-tls")
        assert response == b""
        payload = tls_client_hello(alpn=[CONTROL_MARKER])
        assert await send_to_mux(mux.port, payload) == b"control:" + payload
        assert mux.control_seen == [payload]
    finally:
        await close_mux(mux)


@pytest.mark.asyncio
async def test_multiple_sequential_connections_do_not_restart_listener():
    mux = await start_mux()
    try:
        control_payload = tls_client_hello(sni=CONTROL_MARKER)
        assert await send_to_mux(mux.port, b"first") == b"data:first"
        assert await send_to_mux(mux.port, b"second") == b"data:second"
        assert await send_to_mux(mux.port, control_payload) == b"control:" + control_payload
        assert mux.data_seen == [b"first", b"second"]
        assert mux.control_seen == [control_payload]
    finally:
        await close_mux(mux)


@pytest.mark.asyncio
async def test_listener_stays_alive_after_client_disconnect():
    mux = await start_mux()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", mux.port)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        assert await send_to_mux(mux.port, b"after-close") == b"data:after-close"
    finally:
        await close_mux(mux)
