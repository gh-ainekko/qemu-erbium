#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""Real Erbium UART for the self-hosted MicroBlocks browser IDE.

Bind only to loopback, behind the *private* exe.dev authenticated proxy. Origin
checks prevent cross-site browser use; forwarded headers are not authentication.
There is no HTTP interface for choosing paths, firmware, or shell commands.
"""
import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
from pathlib import Path
import pty
import signal
import struct
import termios
import time
import tty
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

LIMIT = 1024 * 1024
CHUNK = 65536
SESSION = web.AppKey("uart_session", object)
WEB_ROOT = web.AppKey("web_root", Path)
ASSETS_ROOT = web.AppKey("assets_root", Path)
mimetypes.add_type("application/wasm", ".wasm")


async def close_websocket(ws, transport, code, message=b""):
    try:
        # aiohttp's close timeout bounds peer replies, not a stalled socket
        # drain. Bound the entire operation so reset/shutdown cannot hang.
        await asyncio.wait_for(ws.close(code=code, message=message), 2)
    except asyncio.TimeoutError:
        if transport:
            transport.abort()


class Capture:
    """Bound disk usage as well as RAM; binary captures have no added framing."""

    def __init__(self, path, limit=16 * 1024 * 1024):
        self.path, self.limit = Path(path), limit
        self.handle = self.path.open("ab", buffering=0)
        self.size = self.path.stat().st_size

    def write(self, data):
        if self.size + len(data) > self.limit:
            self.handle.close()
            self.path.with_name(self.path.name + ".2").unlink(missing_ok=True)
            old = self.path.with_name(self.path.name + ".1")
            if old.exists():
                old.replace(self.path.with_name(self.path.name + ".2"))
            self.path.replace(old)
            self.handle = self.path.open("wb", buffering=0)
            self.size = 0
        self.handle.write(data)
        self.size += len(data)

    def close(self):
        self.handle.close()


class UART:
    """Bounded nonblocking PTY wire, including short writes and EAGAIN.

    Readers are removed at the exact bound. The websocket sender does not remove
    its chunk until send_bytes/drain completes, so in-flight bytes count too.
    On unplug/reset the old connection's pending bytes are deliberately cleared.
    Unattached board output is captured and drained, never sent to a new client.
    """

    def __init__(self, logs):
        self.loop = asyncio.get_running_loop()
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)
        self.path = os.ttyname(self.slave)
        self.to_board, self.to_client = bytearray(), bytearray()
        self.room, self.data = asyncio.Event(), asyncio.Event()
        self.room.set()
        self.client = None
        self.enabled = False
        self.error = None
        self.reading = self.writing = False
        self.totals = [0, 0]
        self.high_water = [0, 0]
        self.tx_log = Capture(logs / "uart-client-to-emulator.bin")
        self.rx_log = Capture(logs / "uart-emulator-to-client.bin")

    def update(self):
        read = self.enabled and len(self.to_client) < LIMIT
        write = self.enabled and bool(self.to_board)
        if read != self.reading:
            (self.loop.add_reader(self.master, self.io_ready, self.read_ready) if read
             else self.loop.remove_reader(self.master))
            self.reading = read
        if write != self.writing:
            (self.loop.add_writer(self.master, self.io_ready, self.write_ready) if write
             else self.loop.remove_writer(self.master))
            self.writing = write
        (self.room.set if len(self.to_board) < LIMIT else self.room.clear)()
        (self.data.set if self.to_client else self.data.clear)()

    def io_ready(self, operation):
        try:
            operation()
        except Exception as exc:
            # Never leave an errored fd in a busy reader/writer callback loop.
            self.error = exc
            self.enabled = False
            self.update()

    def read_ready(self):
        try:
            data = os.read(self.master, min(CHUNK, LIMIT - len(self.to_client)))
        except (BlockingIOError, InterruptedError):
            return
        if not data:
            raise RuntimeError("UART PTY closed")
        self.rx_log.write(data)
        self.totals[1] += len(data)
        if self.client is not None:
            self.to_client.extend(data)
            self.high_water[1] = max(self.high_water[1], len(self.to_client))
        self.update()

    def write_ready(self):
        try:
            # os.write can return any positive prefix, not necessarily CHUNK.
            sent = os.write(self.master, self.to_board[:CHUNK])
        except (BlockingIOError, InterruptedError):
            return
        if sent:
            self.tx_log.write(self.to_board[:sent])
            self.totals[0] += sent
            del self.to_board[:sent]
        self.update()

    async def receive(self, ws):
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                if len(message.data) > CHUNK:
                    await ws.close(code=1009, message=b"UART message exceeds 64 KiB")
                    return
                offset = 0
                while offset < len(message.data) and self.client is ws:
                    await self.room.wait()
                    if self.client is not ws:
                        return
                    size = min(len(message.data) - offset, LIMIT - len(self.to_board))
                    self.to_board.extend(message.data[offset:offset + size])
                    offset += size
                    self.high_water[0] = max(self.high_water[0], len(self.to_board))
                    self.update()
            elif message.type == WSMsgType.TEXT:
                await ws.close(code=1003, message=b"UART accepts binary bytes only")
                return
            elif message.type == WSMsgType.ERROR:
                return

    async def send(self, ws):
        while self.client is ws:
            await self.data.wait()
            if self.client is not ws:
                return
            if not self.to_client:
                self.data.clear()
                continue
            data = bytes(self.to_client[:CHUNK])
            await ws.send_bytes(data)
            if self.client is not ws:
                return
            del self.to_client[:len(data)]
            self.update()

    def flush(self):
        self.to_board.clear()
        self.to_client.clear()
        # Capture pending board output before unplug discards it. Bound the
        # drain as well: a continuously printing guest must not stall reset.
        remaining = LIMIT
        while remaining:
            try:
                data = os.read(self.master, min(CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                self.rx_log.write(data)
                self.totals[1] += len(data)
            except (BlockingIOError, InterruptedError):
                break
            except OSError as exc:
                self.error = exc
                self.enabled = False
                break
        termios.tcflush(self.slave, termios.TCIOFLUSH)
        self.update()
        # Wake tasks waiting on the previous connection so they can exit.
        self.room.set()
        self.data.set()

    def close(self):
        self.enabled = False
        self.update()
        os.close(self.master)
        os.close(self.slave)
        self.tx_log.close()
        self.rx_log.close()


class Session:
    def __init__(self, repo, logs, elf=None, emu=None, on_fatal=None):
        self.repo, self.logs = Path(repo).resolve(), Path(logs).resolve()
        self.elf = Path(elf or self.repo / "build/smallvm/smallvm.elf").resolve()
        self.emu = Path(emu or self.repo / "dist/bin/erbium_emu").resolve()
        header = self.elf.read_bytes()[:64]
        if len(header) != 64 or header[:6] != b"\x7fELF\x02\x01":
            raise ValueError("firmware must be a little-endian ELF64")
        self.entry = struct.unpack_from("<Q", header, 24)[0]
        self.firmware_sha256 = hashlib.sha256(self.elf.read_bytes()).hexdigest()
        self.emulator_sha256 = hashlib.sha256(self.emu.read_bytes()).hexdigest()
        self.logs.mkdir(parents=True, exist_ok=True)
        self.events = Capture(self.logs / "events.jsonl", limit=1024 * 1024)
        self.stdout = Capture(self.logs / "emulator.stdout.log")
        self.stderr = Capture(self.logs / "emulator.stderr.log")
        self.wire = UART(self.logs)
        self.lock = asyncio.Lock()
        self.proc, self.watch_task = None, None
        self.client_transport = None
        self.log_tasks, self.client_tasks = [], []
        self.resets = 0
        self.started = time.time()
        self.stopping = False
        self.failure = None
        self.on_fatal = on_fatal
        self.last_start = 0
        self.command = [
            str(self.emu), "-elf_load", str(self.elf), "-reset_pc", hex(self.entry),
            "-minions", "1", "-single_thread", "-max_cycles", "-1",
            "-uart_rx_file", self.wire.path, "-uart_tx_file", self.wire.path,
        ]

    def event(self, kind, **details):
        try:
            self.events.write((json.dumps(dict(time=time.time(), kind=kind, **details)) + "\n").encode())
        except OSError as exc:
            # A full disk must not prevent owned children from being reaped.
            logging.error("UART event capture failed: %s", exc)
            self.wire.error = exc

    async def log_stream(self, stream, capture):
        while data := await stream.read(CHUNK):
            capture.write(data)

    async def spawn(self):
        self.event("spawn", command=self.command)
        self.proc = await asyncio.create_subprocess_exec(
            *self.command, cwd=self.repo, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.last_start = time.monotonic()
        self.log_tasks = [
            asyncio.create_task(self.log_stream(self.proc.stdout, self.stdout)),
            asyncio.create_task(self.log_stream(self.proc.stderr, self.stderr)),
        ]
        self.wire.enabled = True
        self.wire.update()

    async def terminate(self):
        self.wire.enabled = False
        self.wire.update()
        proc, self.proc = self.proc, None
        if proc:
            # Kill the owned process group even if its leader has already exited.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            self.event("exit", pid=proc.pid, returncode=proc.returncode)
        if self.log_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*self.log_tasks, return_exceptions=True), 3)
            except asyncio.TimeoutError:
                pass
            self.log_tasks = []

    async def unplug(self, code=1012):
        ws, self.wire.client = self.wire.client, None
        transport, self.client_transport = self.client_transport, None
        tasks, self.client_tasks = self.client_tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if ws:
            await close_websocket(ws, transport, code, b"Erbium UART restarted")
        self.wire.flush()

    async def restart(self, reason="api-reset"):
        async with self.lock:
            self.event("restart", reason=reason)
            self.wire.enabled = False
            self.wire.update()
            await self.unplug()
            await self.terminate()
            self.wire.flush()
            self.resets += 1
            try:
                await self.spawn()
            except Exception as exc:
                self.fail(exc)
                raise

    async def watch(self):
        while not self.stopping:
            await asyncio.sleep(.25)
            if self.stopping:
                return
            if self.wire.error:
                raise RuntimeError("UART I/O failed") from self.wire.error
            for task in self.log_tasks:
                if task.done() and not task.cancelled() and task.exception():
                    raise task.exception()
            if (self.proc and self.proc.returncode is not None
                    and time.monotonic() - self.last_start >= 3):
                await self.restart("emulator-exit")

    async def start(self):
        await self.spawn()
        self.watch_task = asyncio.create_task(self.watch())
        self.watch_task.add_done_callback(self.watch_finished)

    def watch_finished(self, task):
        if not task.cancelled() and task.exception():
            self.fail(task.exception())

    def fail(self, exc):
        already_failed = self.failure is not None
        self.failure = str(exc) or type(exc).__name__
        self.wire.enabled = False
        self.wire.update()
        logging.error("UART supervisor failed: %s", exc)
        if self.on_fatal and not already_failed:
            self.on_fatal()

    def status(self):
        return dict(
            started=self.started, connected=self.wire.client is not None,
            emulator_running=bool(self.proc and self.proc.returncode is None and not self.failure),
            error=self.failure,
            emulator_pid=self.proc.pid if self.proc else None,
            emulator_returncode=self.proc.returncode if self.proc else None,
            emulator_restarts=self.resets,
            bytes_to_emulator=self.wire.totals[0], bytes_from_emulator=self.wire.totals[1],
            queued_to_emulator=len(self.wire.to_board), queued_to_client=len(self.wire.to_client),
            high_water_to_emulator=self.wire.high_water[0],
            high_water_to_client=self.wire.high_water[1], queue_limit=LIMIT,
            websocket_max_message=CHUNK, logs=str(self.logs), command=self.command,
            firmware_sha256=self.firmware_sha256, emulator_sha256=self.emulator_sha256,
        )

    async def close(self):
        self.stopping = True
        if self.watch_task:
            self.watch_task.cancel()
            await asyncio.gather(self.watch_task, return_exceptions=True)
        async with self.lock:
            self.wire.enabled = False
            self.wire.update()
            await self.unplug(code=1001)
            await self.terminate()
            self.wire.close()
            for capture in (self.events, self.stdout, self.stderr):
                capture.close()


def same_origin(request):
    """Validate Origin against the private proxy's external host and protocol."""
    def one(name, default=None):
        values = request.headers.getall(name, [])
        if len(values) > 1 or (values and "," in values[0]):
            raise web.HTTPForbidden(text="Ambiguous origin headers")
        return values[0] if values else default

    origin = one("Origin")
    host = one("X-Forwarded-Host", one("Host", ""))
    scheme = one("X-Forwarded-Proto", "http")
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        raise web.HTTPForbidden(text="Cross-site access denied")
    if not origin:
        try:
            local = ipaddress.ip_address(request.remote).is_loopback
        except (ValueError, TypeError):
            local = False
        if local:
            return  # localhost tools; remote requests still need private proxy auth
        raise web.HTTPForbidden(text="Origin required")
    try:
        parsed = urlsplit(origin)
        expected = urlsplit(scheme + "://" + host)
        valid = (
            scheme in ("http", "https") and host and expected.hostname
            and expected.netloc == host and not expected.path and not expected.query
            and not expected.fragment and not expected.username and not expected.password
            and parsed.scheme == scheme and parsed.netloc.lower() == host.lower()
            and parsed.hostname and not parsed.username and not parsed.password
            and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
            and parsed.port == expected.port
        )
    except ValueError:
        valid = False
    if not valid:
        raise web.HTTPForbidden(text="Cross-origin UART access denied")


@web.middleware
async def boundary(request, handler):
    # A user following an IDE link from another site legitimately sends
    # Sec-Fetch-Site: cross-site. Static GET/HEAD requests are read-only and
    # need no Origin check; the private proxy still authenticates the caller.
    # Keep all APIs and UART strict (including GET/HEAD), and protect every
    # non-read-only method against CSRF.
    if (request.method not in ("GET", "HEAD") or request.path == "/uart"
            or request.path.startswith("/api/")):
        same_origin(request)
    return await handler(request)


async def response_headers(request, response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    # /api and WS responses should never be replayed from a browser cache.
    if request.path.startswith(("/api/", "/uart")):
        response.headers["Cache-Control"] = "no-store"


async def websocket(request):
    session = request.app[SESSION]
    ws = web.WebSocketResponse(
        # Distro aiohttp 3.9 rejects >= max_msg_size, so permit the inclusive
        # 64 KiB bound here and enforce it explicitly in UART.receive too.
        max_msg_size=CHUNK + 1, compress=False, heartbeat=30, timeout=2,
    )
    async with session.lock:
        if (session.stopping or session.failure or not session.proc
                or session.proc.returncode is not None):
            raise web.HTTPServiceUnavailable(text="Emulator unavailable")
        if session.wire.client is not None:
            raise web.HTTPConflict(text="Erbium UART already has a client")
        session.wire.flush()
        session.wire.client = ws  # reserve BEFORE the upgrade awaits
        session.client_transport = request.transport
        session.wire.update()
        try:
            await ws.prepare(request)
        except BaseException:
            session.wire.client = None
            session.client_transport = None
            raise
        session.event("connect")
        tasks = [asyncio.create_task(session.wire.send(ws)),
                 asyncio.create_task(session.wire.receive(ws))]
        session.client_tasks = tasks
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception():
                raise task.exception()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        owned = session.wire.client is ws
        if owned:
            session.wire.client = None
            session.client_transport = None
            session.client_tasks = []
            session.wire.flush()
        session.event("disconnect")
        await close_websocket(ws, request.transport,
                              1000 if owned else (1001 if session.stopping else 1012))
    return ws


async def status(request):
    return web.json_response(request.app[SESSION].status())


async def reset(request):
    # No body/commands accepted. Explicitly reject payloads, including chunked.
    if request.can_read_body:
        raise web.HTTPBadRequest(text="Reset accepts no request body")
    await request.app[SESSION].restart()
    return web.json_response(request.app[SESSION].status())


async def static(request):
    name = request.match_info.get("name", "index.html")
    if request.path == "/example.ubp":
        # Fixed, read-only sample; this is not a caller-selected filesystem path.
        return web.FileResponse(
            request.app[SESSION].repo / "smallvm/examples/Erbium Showcase.ubp")
    assets = request.path.startswith("/assets/")
    root = request.app[ASSETS_ROOT if assets else WEB_ROOT]
    if not assets and name not in ("index.html", "erbium.js"):
        raise web.HTTPNotFound()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file() or any(
            part.startswith(".") for part in Path(name).parts):
        raise web.HTTPNotFound()
    # FileResponse may automatically choose a .gz sibling. Check that path too.
    gzip = path.with_name(path.name + ".gz")
    if gzip.exists() and not gzip.resolve().is_relative_to(root):
        raise web.HTTPNotFound()
    return web.FileResponse(path)


def make_app(repo, logs, web_root=None, assets=None, elf=None, emu=None, on_fatal=None):
    repo = Path(repo).resolve()
    app = web.Application(middlewares=[boundary], client_max_size=1024)
    app[WEB_ROOT] = Path(web_root or repo / "smallvm/web").resolve()
    app[ASSETS_ROOT] = Path(assets or repo / "build/smallvm/web").resolve()

    async def lifetime(app):
        session = Session(repo, logs, elf, emu, on_fatal)
        app[SESSION] = session
        try:
            await session.start()
            yield
        finally:
            await session.close()

    app.cleanup_ctx.append(lifetime)
    async def shutdown(app):
        session = app.get(SESSION)
        if session:
            # systemd signals the whole cgroup, including the child. Do not
            # auto-restart it while aiohttp is draining active HTTP handlers.
            session.stopping = True
            async with session.lock:
                session.wire.enabled = False
                session.wire.update()
                await session.unplug(code=1001)

    app.on_shutdown.append(shutdown)
    app.on_response_prepare.append(response_headers)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/reset", reset)
    app.router.add_get("/uart", websocket)
    app.router.add_get("/", static)
    app.router.add_get("/assets/{name:.*}", static)
    app.router.add_get("/{name}", static)
    return app


def main():
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=repo)
    parser.add_argument("--web", type=Path)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--logs", type=Path)
    parser.add_argument("--elf", type=Path)
    parser.add_argument("--emu", type=Path)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    # CLI and service both use the distro interpreter/aiohttp, no network deps.
    os.umask(0o077)
    failed = False

    def fatal():
        nonlocal failed
        failed = True
        # aiohttp's signal handler closes sockets, PTYs and owned children.
        # After cleanup, return nonzero so systemd Restart=on-failure applies.
        os.kill(os.getpid(), signal.SIGTERM)

    web.run_app(make_app(
        args.repo, args.logs or args.repo / "build/smallvm-web/current",
        args.web, args.assets, args.elf, args.emu,
        on_fatal=fatal,
    ), host="127.0.0.1", port=args.port, shutdown_timeout=5)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
