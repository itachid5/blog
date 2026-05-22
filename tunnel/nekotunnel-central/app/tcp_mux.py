#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import os
import signal
import socket
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    name: str
    host: str
    port: int


def log(message: str) -> None:
    print(f"[nekotunnel-mux] {message}", flush=True)


CONTROL_MARKER = "nekotunnel-control"
_CONNECTION_IDS = itertools.count(1)


def is_tls_client_hello(data: bytes) -> bool:
    return len(data) >= 5 and data[0] == 0x16 and data[1] == 0x03 and 0x00 <= data[2] <= 0x04


def _read_u16(data: bytes, offset: int) -> tuple[int, int]:
    if offset + 2 > len(data):
        raise ValueError("truncated-u16")
    return int.from_bytes(data[offset : offset + 2], "big"), offset + 2


def _read_u24(data: bytes, offset: int) -> tuple[int, int]:
    if offset + 3 > len(data):
        raise ValueError("truncated-u24")
    return int.from_bytes(data[offset : offset + 3], "big"), offset + 3


def _parse_client_hello_markers(data: bytes) -> tuple[list[str], list[str]]:
    if not is_tls_client_hello(data):
        return [], []
    record_length, offset = _read_u16(data, 3)
    record_end = min(len(data), 5 + record_length)
    if record_end < 9 or data[5] != 0x01:
        return [], []
    handshake_length, offset = _read_u24(data, 6)
    hello_end = min(record_end, 9 + handshake_length)
    offset = 9 + 2 + 32
    if offset + 1 > hello_end:
        return [], []
    session_len = data[offset]
    offset += 1 + session_len
    cipher_len, offset = _read_u16(data, offset)
    offset += cipher_len
    if offset + 1 > hello_end:
        return [], []
    compression_len = data[offset]
    offset += 1 + compression_len
    if offset + 2 > hello_end:
        return [], []
    extensions_len, offset = _read_u16(data, offset)
    extensions_end = min(hello_end, offset + extensions_len)
    sni_names: list[str] = []
    alpn_protocols: list[str] = []
    while offset + 4 <= extensions_end:
        ext_type, offset = _read_u16(data, offset)
        ext_len, offset = _read_u16(data, offset)
        ext_end = min(extensions_end, offset + ext_len)
        ext = data[offset:ext_end]
        if ext_type == 0 and len(ext) >= 2:
            list_len = int.from_bytes(ext[0:2], "big")
            pos = 2
            list_end = min(len(ext), 2 + list_len)
            while pos + 3 <= list_end:
                name_type = ext[pos]
                name_len = int.from_bytes(ext[pos + 1 : pos + 3], "big")
                pos += 3
                name = ext[pos : pos + name_len]
                pos += name_len
                if name_type == 0:
                    with contextlib.suppress(UnicodeDecodeError):
                        sni_names.append(name.decode("idna"))
        elif ext_type == 16 and len(ext) >= 2:
            list_len = int.from_bytes(ext[0:2], "big")
            pos = 2
            list_end = min(len(ext), 2 + list_len)
            while pos + 1 <= list_end:
                proto_len = ext[pos]
                pos += 1
                proto = ext[pos : pos + proto_len]
                pos += proto_len
                with contextlib.suppress(UnicodeDecodeError):
                    alpn_protocols.append(proto.decode("ascii"))
        offset = ext_end
    return sni_names, alpn_protocols


def classify_route(data: bytes) -> tuple[str, str]:
    if not is_tls_client_hello(data):
        return "data", "non-tls"
    try:
        sni_names, alpn_protocols = _parse_client_hello_markers(data)
    except ValueError:
        return "data", "ambiguous-tls-fallback-data"
    if CONTROL_MARKER in alpn_protocols:
        return "control", "control-marker-alpn"
    if CONTROL_MARKER in sni_names:
        return "control", "control-marker-sni"
    return "data", "ambiguous-tls-fallback-data"


def tune_socket(sock: socket.socket | None) -> None:
    if sock is None:
        return
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)


async def open_target(target: Target, attempts: int, delay: float) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            reader, writer = await asyncio.open_connection(target.host, target.port)
            tune_socket(writer.get_extra_info("socket"))
            return reader, writer
        except OSError as exc:
            last_error = exc
            if attempt < attempts:
                await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                return "eof"
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError) as exc:
        return exc.__class__.__name__
    finally:
        writer.close()


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    control: Target,
    data: Target,
    target_attempts: int,
    target_retry_delay: float,
) -> None:
    peer = writer.get_extra_info("peername")
    connection_id = next(_CONNECTION_IDS)
    tune_socket(writer.get_extra_info("socket"))
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        initial = await asyncio.wait_for(reader.read(4096), timeout=5)
        route_name, route_reason = classify_route(initial)
        target = control if route_name == "control" else data
        route = "tls/control" if target == control else "data/proxy"
        log(f"connection id={connection_id} peer={peer} route={route} reason={route_reason}")
        upstream_reader, upstream_writer = await open_target(target, target_attempts, target_retry_delay)
        if initial:
            upstream_writer.write(initial)
            await upstream_writer.drain()
        reasons = await asyncio.gather(
            pipe(reader, upstream_writer),
            pipe(upstream_reader, writer),
            return_exceptions=True,
        )
        log(f"connection id={connection_id} peer={peer} route={route} closed reason={reasons[0]}/{reasons[1]}")
    except asyncio.TimeoutError:
        log(f"connection id={connection_id} peer={peer} closed reason=initial-timeout")
    except OSError as exc:
        log(f"connection id={connection_id} peer={peer} closed reason=target-connect-failed error={exc}")
    except Exception as exc:
        log(f"connection id={connection_id} peer={peer} closed reason=handler-error error={exc.__class__.__name__}: {exc}")
    finally:
        if upstream_writer is not None:
            upstream_writer.close()
            with contextlib.suppress(Exception):
                await upstream_writer.wait_closed()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def check_port(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except OSError:
        return False
    except asyncio.TimeoutError:
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


async def health_loop(bind_host: str, listen_port: int, control: Target, data: Target, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        mux_alive = True
        control_alive, data_alive = await asyncio.gather(
            check_port(control.host, control.port),
            check_port(data.host, data.port),
        )
        if not control_alive or not data_alive:
            log(
                "health "
                f"mux_listener={'alive' if mux_alive else 'down'} "
                f"control={'alive' if control_alive else 'down'} "
                f"data={'alive' if data_alive else 'down'} "
                f"listen={bind_host}:{listen_port}"
            )


async def serve(args: argparse.Namespace) -> None:
    control = Target("control", args.control_host, args.control_port)
    data = Target("data", args.data_host, args.data_port)
    server = await asyncio.start_server(
        lambda reader, writer: handle_client(reader, writer, control, data, args.target_attempts, args.target_retry_delay),
        args.host,
        args.port,
        reuse_address=True,
    )
    sockets = server.sockets or []
    for sock in sockets:
        tune_socket(sock)
    log(f"mux started on port {args.port}")
    log(f"control target {control.host}:{control.port}")
    log(f"data target {data.host}:{data.port}")
    health_task = asyncio.create_task(health_loop(args.host, args.port, control, data, args.health_interval))
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    async with server:
        serve_task = asyncio.create_task(server.serve_forever())
        await stop.wait()
        serve_task.cancel()
        health_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task
        with contextlib.suppress(asyncio.CancelledError):
            await health_task


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NekoTunnel TCP multiplexer")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    parser.add_argument("--control-host", default="127.0.0.1")
    parser.add_argument("--control-port", type=int, default=7000)
    parser.add_argument("--data-host", default="127.0.0.1")
    parser.add_argument("--data-port", type=int, default=6000)
    parser.add_argument("--target-attempts", type=int, default=5)
    parser.add_argument("--target-retry-delay", type=float, default=0.5)
    parser.add_argument("--health-interval", type=float, default=30.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        log(f"mux process exit fatal error={exc.__class__.__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
