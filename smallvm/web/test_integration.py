#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""Real UART/HTTP integration on isolated :8001 (never attach to :8000).

Requires built SmallVM and emulator. --echo-elf adds real MMIO all-byte echo.
Outputs process logs, raw captures and a provenance/result JSON.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import socket
import subprocess
import time

import aiohttp
from yarl import URL

from server import CHUNK


def decode(data):
    result, pos = [], 0
    while pos < len(data):
        assert data[pos] in (250, 251), f"unframed UART byte {pos}"
        if len(data) - pos < 3:
            break
        size = 0
        header = 3
        if data[pos] == 251:
            if len(data) - pos < 5:
                break
            size = int.from_bytes(data[pos + 3:pos + 5], "little")
            header = 5
        if len(data) - pos < size + header:
            break
        result.append((data[pos + 1], data[pos + 2], bytes(data[pos + header:pos + header + size])))
        pos += size + header
    return result


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def status(client, base):
    async with client.get(base + "/api/status") as response:
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        return await response.json()


async def handshake(ws):
    received = bytearray()
    deadline = time.monotonic() + 20
    # Let the bare-metal startup run; retry as a real serial IDE would.
    while time.monotonic() < deadline:
        await ws.send_bytes(bytes([250, 26, 73, 250, 12, 0]))
        end = time.monotonic() + 1
        while time.monotonic() < end:
            try:
                message = await ws.receive(timeout=end - time.monotonic())
            except asyncio.TimeoutError:
                break
            assert message.type == aiohttp.WSMsgType.BINARY, message
            assert message.data, "empty/metadata websocket frame"
            received.extend(message.data)
            frames = decode(received)
            if (any(typ == 26 and ident == 73 for typ, ident, _ in frames)
                    and any(typ == 22 and b"v416 Erbium" in body for typ, _, body in frames)):
                return bytes(received)
    raise AssertionError("real UART ping/version timed out")


async def denied_ws(client, base, code, **kwargs):
    try:
        ws = await client.ws_connect(base + "/uart", **kwargs)
    except aiohttp.WSServerHandshakeError as exc:
        assert exc.status == code, exc.status
    else:
        await ws.close()
        raise AssertionError(f"websocket unexpectedly accepted; wanted {code}")


class Server:
    def __init__(self, args, elf, name):
        self.args, self.elf = args, elf
        self.output = args.output / name / str(time.time_ns())
        self.output.mkdir(parents=True, exist_ok=True)
        self.pid = None
        self.expected_exit = 0

    async def __aenter__(self):
        # An occupied port is a hard error, not permission to reset its owner.
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", self.args.port))
        self.handle = (self.output / "server.log").open("wb")
        self.proc = subprocess.Popen([
            "/usr/bin/python3", str(self.args.repo / "smallvm/web/server.py"),
            "--repo", str(self.args.repo), "--web", str(self.args.web),
            "--assets", str(self.args.assets), "--logs", str(self.output),
            "--elf", str(self.elf), "--emu", str(self.args.emu),
            "--port", str(self.args.port),
        ], stdout=self.handle, stderr=subprocess.STDOUT, start_new_session=True)
        self.base = f"http://127.0.0.1:{self.args.port}"
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        try:
            for _ in range(200):
                assert self.proc.poll() is None, "server startup failed; see server.log"
                try:
                    self.initial = await status(self.client, self.base)
                    self.pid = self.initial["emulator_pid"]
                    return self
                except aiohttp.ClientConnectorError:
                    await asyncio.sleep(.05)
            raise AssertionError("server startup timeout")
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        await self.client.close()
        self.proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.to_thread(self.proc.wait, 10)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            await asyncio.to_thread(self.proc.wait)
            raise AssertionError("server failed graceful shutdown")
        finally:
            self.handle.close()
        assert self.proc.returncode == self.expected_exit, f"server exit {self.proc.returncode}"
        assert not self.pid or not alive(self.pid), "owned emulator leaked on shutdown"


async def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result = dict(status="FAIL", port=args.port, elf=str(args.elf), emulator=str(args.emu))
    try:
        async with Server(args, args.elf, "protocol") as server:
            client, base = server.client, server.base
            initial = server.initial
            result["initial"] = initial
            assert initial["emulator_running"] and not initial["connected"]
            assert initial["firmware_sha256"] == hashlib.sha256(args.elf.read_bytes()).hexdigest()
            # A link clicked from chat is a legitimate cross-site document
            # navigation. Static GET/HEAD must work, with framing policy intact.
            navigation_headers = {
                "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            }
            for method in ("GET", "HEAD"):
                async with client.request(method, base + "/", headers=navigation_headers) as r:
                    assert r.status == 200
                    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
            for method, path in (("GET", "/api/status"), ("POST", "/api/reset")):
                async with client.request(method, base + path, headers=navigation_headers) as r:
                    assert r.status == 403
            await denied_ws(client, base, 403, headers=navigation_headers)
            # Test native localhost tools, browser origin, and forwarded proxy origin.
            for origin in ("null", "https://evil.example", base + "/invalid"):
                await denied_ws(client, base, 403, headers={"Origin": origin})
                async with client.post(base + "/api/reset", headers={"Origin": origin}) as r:
                    assert r.status == 403
            proxy_headers = {
                "Origin": "https://erbium-qemu.exe.xyz",
                "X-Forwarded-Host": "erbium-qemu.exe.xyz", "X-Forwarded-Proto": "https",
            }
            ws = await client.ws_connect(base + "/uart", headers=proxy_headers)
            first = await handshake(ws)
            assert (await status(client, base))["connected"]
            await denied_ws(client, base, 409)
            # Reset does not accept firmware selection or arbitrary commands.
            async with client.post(base + "/api/reset", data=b"command=rm") as r:
                assert r.status == 400
            async with client.post(base + "/api/reset", headers=proxy_headers) as r:
                assert r.status == 200
                reset_status = await r.json()
            message = await ws.receive(timeout=5)
            assert message.type == aiohttp.WSMsgType.CLOSE and message.data == 1012, message
            assert reset_status["emulator_pid"] != initial["emulator_pid"]
            assert not alive(initial["emulator_pid"]), "reset leaked old emulator"
            assert reset_status["emulator_restarts"] == 1 and not reset_status["connected"]
            server.pid = reset_status["emulator_pid"]
            ws = await client.ws_connect(base + "/uart", headers={"Origin": base})
            second = await handshake(ws)
            await ws.close()
            for _ in range(100):
                if not (await status(client, base))["connected"]:
                    break
                await asyncio.sleep(.01)
            # A new client after unplug uses the same emulator (no implicit reset).
            ws = await client.ws_connect(base + "/uart")
            await handshake(ws)
            assert (await status(client, base))["emulator_pid"] == server.pid
            await ws.send_str("status")
            message = await ws.receive(timeout=5)
            assert message.type == aiohttp.WSMsgType.CLOSE and message.data == 1003, message
            await asyncio.sleep(.05)
            ws = await client.ws_connect(base + "/uart")
            await ws.send_bytes(b"x" * (CHUNK + 1))
            message = await ws.receive(timeout=5)
            assert message.type == aiohttp.WSMsgType.CLOSE and message.data == 1009, message
            await asyncio.sleep(.05)
            # Owned emulator exit is detected, UART closed, and automatically restarted.
            ws = await client.ws_connect(base + "/uart")
            dead_pid = server.pid
            os.kill(dead_pid, signal.SIGKILL)
            message = await ws.receive(timeout=10)
            assert message.type == aiohttp.WSMsgType.CLOSE and message.data == 1012, message
            for _ in range(100):
                crashed = await status(client, base)
                if crashed["emulator_running"] and crashed["emulator_pid"] != dead_pid:
                    break
                await asyncio.sleep(.05)
            assert crashed["emulator_pid"] != dead_pid and crashed["emulator_running"]
            assert not alive(dead_pid)
            server.pid = crashed["emulator_pid"]
            ws = await client.ws_connect(base + "/uart")
            await handshake(ws)
            await ws.close()
            result.update(version_uart_hex=first.hex(), after_reset_uart_hex=second.hex())
            result["protocol_final"] = await status(client, base)
            # No source files, dotfiles, directory listing, or escape via symlinks.
            for path in ("/server.py", "/test_server.py", "/assets/", "/assets/.hidden",
                         "/assets/%2e%2e/%2e%2e/smallvm.elf"):
                async with client.get(URL(base + path, encoded=True)) as r:
                    assert r.status == 404, (path, r.status)
            for filename in ("uart-client-to-emulator.bin", "uart-emulator-to-client.bin"):
                assert (server.output / filename).stat().st_size > 0
            # Simulate systemd stopping both the server and its child while an
            # idle UART remains open. No shutdown-time auto-restart is allowed.
            ws = await client.ws_connect(base + "/uart")
            os.kill(server.pid, signal.SIGTERM)
            server.proc.send_signal(signal.SIGTERM)
            message = await ws.receive(timeout=5)
            assert message.type == aiohttp.WSMsgType.CLOSE and message.data == 1001, message
            await asyncio.to_thread(server.proc.wait, 10)
            assert not alive(server.pid)
            events = [json.loads(line) for line in (server.output / "events.jsonl").read_text().splitlines()]
            assert sum(event["kind"] == "spawn" for event in events) == 3
        if args.echo_elf:
            async with Server(args, args.echo_elf, "echo") as server:
                # Startup READY is captured while unattached; drain before attaching.
                await asyncio.sleep(.25)
                ws = await server.client.ws_connect(server.base + "/uart")
                payload = bytes(range(256)) * 1024
                received = bytearray()

                async def upload():
                    for offset in range(0, len(payload), CHUNK):
                        await ws.send_bytes(payload[offset:offset + CHUNK])

                task = asyncio.create_task(upload())
                try:
                    async with asyncio.timeout(60):
                        while len(received) < len(payload):
                            message = await ws.receive()
                            assert message.type == aiohttp.WSMsgType.BINARY and message.data, message
                            received.extend(message.data)
                        await task
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                assert received == payload, "all-byte UART echo corrupted"
                await ws.close()
                final = await status(server.client, server.base)
                assert final["queued_to_emulator"] <= final["queue_limit"]
                assert final["queued_to_client"] <= final["queue_limit"]
                assert (server.output / "uart-client-to-emulator.bin").read_bytes() == payload
                assert (server.output / "uart-emulator-to-client.bin").read_bytes().endswith(payload)
                result.update(echo_bytes=len(payload), echo_final=final)
                # Force a real capture I/O failure in this isolated server,
                # without filling the VM disk. The watchdog must be observed,
                # close/reap the session, and exit nonzero for systemd restart.
                ws = await server.client.ws_connect(server.base + "/uart")
                resource.prlimit(server.proc.pid, resource.RLIMIT_FSIZE, (8192, 8192))
                server.expected_exit = 1
                await ws.send_bytes(b"capture-failure")
                message = await ws.receive(timeout=10)
                assert message.type == aiohttp.WSMsgType.CLOSE and message.data == 1001, message
                await asyncio.to_thread(server.proc.wait, 10)
                assert server.proc.returncode == 1
                assert not alive(server.pid)
                assert "UART supervisor failed" in (server.output / "server.log").read_text()
                result["fatal_watch_exit"] = server.proc.returncode
        result["status"] = "PASS"
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


def main():
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=repo)
    parser.add_argument("--web", type=Path, default=repo / "smallvm/web")
    parser.add_argument("--assets", type=Path, default=repo / "build/smallvm/web")
    parser.add_argument("--elf", type=Path, default=repo / "build/smallvm/smallvm.elf")
    parser.add_argument("--emu", type=Path, default=repo / "dist/bin/erbium_emu")
    parser.add_argument("--echo-elf", type=Path)
    parser.add_argument("--output", type=Path, default=repo / "build/smallvm-web-test")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    if args.port == 8000:
        parser.error("refusing the production port")
    for name in ("repo", "web", "assets", "elf", "emu", "output", "echo_elf"):
        if getattr(args, name):
            setattr(args, name, getattr(args, name).resolve())
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
