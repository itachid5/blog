import asyncio
import contextlib
import socket
from dataclasses import dataclass

import pytest

from app.tcp_mux import Target, handle_client


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
async def test_tls_like_first_bytes_route_to_control_port():
    mux = await start_mux()
    try:
        payload = b"\x16\x03\x03hello"
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
        payload = b"RDP-data"
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
        payload = b"\x16\x03\x01control"
        assert await send_to_mux(mux.port, payload) == b"control:" + payload
        assert mux.control_seen == [payload]
    finally:
        await close_mux(mux)


@pytest.mark.asyncio
async def test_multiple_sequential_connections_do_not_restart_listener():
    mux = await start_mux()
    try:
        assert await send_to_mux(mux.port, b"first") == b"data:first"
        assert await send_to_mux(mux.port, b"second") == b"data:second"
        assert await send_to_mux(mux.port, b"\x16\x03\x03third") == b"control:\x16\x03\x03third"
        assert mux.data_seen == [b"first", b"second"]
        assert mux.control_seen == [b"\x16\x03\x03third"]
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
