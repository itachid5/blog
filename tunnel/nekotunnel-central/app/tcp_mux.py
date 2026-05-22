#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
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


def is_tls_client_hello(data: bytes) -> bool:
    return len(data) >= 3 and data[0] == 0x16 and data[1] == 0x03 and 0x00 <= data[2] <= 0x04


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
    tune_socket(writer.get_extra_info("socket"))
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        initial = await asyncio.wait_for(reader.read(8), timeout=5)
        target = control if is_tls_client_hello(initial) else data
        route = "tls/control" if target == control else "data/proxy"
        log(f"connection peer={peer} route={route}")
        upstream_reader, upstream_writer = await open_target(target, target_attempts, target_retry_delay)
        if initial:
            upstream_writer.write(initial)
            await upstream_writer.drain()
        reasons = await asyncio.gather(
            pipe(reader, upstream_writer),
            pipe(upstream_reader, writer),
            return_exceptions=True,
        )
        log(f"connection peer={peer} route={route} closed reason={reasons[0]}/{reasons[1]}")
    except asyncio.TimeoutError:
        log(f"connection peer={peer} closed reason=initial-timeout")
    except OSError as exc:
        log(f"connection peer={peer} closed reason=target-connect-failed error={exc}")
    except Exception as exc:
        log(f"connection peer={peer} closed reason=handler-error error={exc.__class__.__name__}: {exc}")
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
