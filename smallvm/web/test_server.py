#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""Bounded wire/unit tests. Uses owned raw PTYs, never the live service."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import WSMessage, WSMsgType
from aiohttp.test_utils import make_mocked_request
from aiohttp import web
from multidict import CIMultiDict

from server import Capture, CHUNK, LIMIT, UART, close_websocket, same_origin


class Origins(unittest.TestCase):
    def check(self, **headers):
        same_origin(make_mocked_request("POST", "/api/reset", headers=headers))

    def test_local_browser(self):
        self.check(Host="localhost:8001", Origin="http://localhost:8001")

    def test_private_proxy(self):
        self.check(**{
            "Host": "127.0.0.1:8000", "Origin": "https://erbium-qemu.exe.xyz",
            "X-Forwarded-Host": "erbium-qemu.exe.xyz", "X-Forwarded-Proto": "https",
        })

    def test_invalid_origins(self):
        for origin in ("null", "https://evil.example", "http://[", "http://",
                       "http://localhost:8001/path", "http://localhost:8001?q=x",
                       "http://localhost:8001#x", "https://localhost:8001",
                       "http://x@localhost:8001", "http://localhost:8001, http://evil"):
            with self.subTest(origin=origin), self.assertRaises(web.HTTPForbidden):
                self.check(Host="localhost:8001", Origin=origin)

    def test_ambiguous_proxy_headers(self):
        for headers in (
            {"X-Forwarded-Host": "localhost:8001, evil"},
            {"X-Forwarded-Proto": "http, https"},
            {"Sec-Fetch-Site": "cross-site"},
            {"X-Forwarded-Host": "localhost:8001/path"},
            {"X-Forwarded-Host": "x@localhost:8001"},
        ):
            with self.subTest(headers=headers), self.assertRaises(web.HTTPForbidden):
                self.check(Host="localhost:8001", Origin="http://localhost:8001", **headers)
        with self.assertRaises(web.HTTPForbidden):
            self.check(Origin="http://localhost:8001")

    def test_duplicate_origin(self):
        headers = CIMultiDict([("Host", "localhost:8001"),
                               ("Origin", "http://localhost:8001"),
                               ("Origin", "http://evil.example")])
        with self.assertRaises(web.HTTPForbidden):
            same_origin(make_mocked_request("GET", "/uart", headers=headers))


class BinaryClient:
    def __init__(self, data):
        self.payload = data

    async def __aiter__(self):
        for offset in range(0, len(self.payload), CHUNK):
            yield WSMessage(WSMsgType.BINARY, self.payload[offset:offset + CHUNK], "")


class Wire(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="smallvm-web-wire-")
        self.wire = UART(Path(self.temp.name))
        self.wire.enabled = True
        os.set_blocking(self.wire.slave, False)

    async def asyncTearDown(self):
        self.wire.close()
        self.temp.cleanup()

    async def test_duplex_exact_bounds_and_partial_writes(self):
        wire = self.wire
        tx = bytes(range(256)) * 10000
        rx = bytes(reversed(range(256))) * 9000
        wire.client = client = BinaryClient(tx)
        wire.update()
        upload = asyncio.create_task(wire.receive(client))
        # Deterministically inject a short write and EAGAIN; then use real PTY.
        original_write = os.write
        call_count = 0

        def short_write(fd, data):
            nonlocal call_count
            if fd == wire.master:
                call_count += 1
                if call_count % 7 == 0:
                    raise BlockingIOError()
                data = data[:31 if call_count % 3 == 0 else 4096]
            return original_write(fd, data)

        to_board, to_client = bytearray(), bytearray()
        offset = 0
        saturated = False
        deadline = asyncio.get_running_loop().time() + 20
        with patch("server.os.write", short_write):
            while len(to_board) < len(tx) or len(to_client) < len(rx):
                self.assertLess(asyncio.get_running_loop().time(), deadline, "wire stalled")
                if offset < len(rx):
                    try:
                        offset += os.write(wire.slave, rx[offset:offset + CHUNK])
                    except BlockingIOError:
                        pass
                self.assertLessEqual(len(wire.to_board), LIMIT)
                self.assertLessEqual(len(wire.to_client), LIMIT)
                saturated |= len(wire.to_board) == LIMIT and len(wire.to_client) == LIMIT
                # Pause both endpoints until both exact limits are reached.
                if saturated:
                    try:
                        to_board.extend(os.read(wire.slave, CHUNK))
                    except BlockingIOError:
                        pass
                    to_client.extend(wire.to_client[:CHUNK])
                    del wire.to_client[:CHUNK]
                    wire.update()
                await asyncio.sleep(.0001)
            await asyncio.wait_for(upload, 1)
        self.assertTrue(saturated, "did not exercise both bounds")
        self.assertEqual(to_board, tx)
        self.assertEqual(to_client, rx)
        self.assertEqual(wire.high_water, [LIMIT, LIMIT])
        self.assertEqual((Path(self.temp.name) / "uart-client-to-emulator.bin").read_bytes(), tx)
        self.assertEqual((Path(self.temp.name) / "uart-emulator-to-client.bin").read_bytes(), rx)

    async def test_send_keeps_inflight_bytes_bounded(self):
        wire = self.wire
        stalled = asyncio.Event()
        release = asyncio.Event()

        class Sender:
            async def send_bytes(self, data):
                self.data = data
                stalled.set()
                await release.wait()

        wire.client = sender = Sender()
        wire.to_client.extend(b"x" * LIMIT)
        wire.update()
        task = asyncio.create_task(wire.send(sender))
        try:
            await asyncio.wait_for(stalled.wait(), 1)
            self.assertEqual(len(wire.to_client), LIMIT)
            self.assertFalse(wire.reading)
            self.assertEqual(len(sender.data), CHUNK)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_flush_and_unattached_output(self):
        wire = self.wire
        wire.update()
        os.write(wire.slave, bytes(range(256)))
        for _ in range(100):
            if wire.totals[1] == 256:
                break
            await asyncio.sleep(.001)
        self.assertEqual(wire.totals[1], 256)
        self.assertFalse(wire.to_client)
        wire.to_board.extend(b"old-input")
        wire.to_client.extend(b"old-output")
        wire.flush()
        self.assertFalse(wire.to_board)
        self.assertFalse(wire.to_client)

    async def test_permanent_io_error_removes_callbacks(self):
        wire = self.wire
        wire.update()
        with patch("server.os.read", side_effect=OSError("permanent error")):
            wire.io_ready(wire.read_ready)
        self.assertIsInstance(wire.error, OSError)
        self.assertFalse(wire.enabled)
        self.assertFalse(wire.reading)
        self.assertFalse(wire.writing)

    async def test_stalled_close_aborts_transport(self):
        class Stalled:
            async def close(self, **kwargs):
                await asyncio.Future()

        class Transport:
            aborted = False

            def abort(self):
                self.aborted = True

        transport = Transport()
        await asyncio.wait_for(close_websocket(Stalled(), transport, 1012), 3)
        self.assertTrue(transport.aborted)


class Logging(unittest.TestCase):
    def test_bounded_capture_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "uart.bin"
            capture = Capture(path, limit=8)
            try:
                for value in (b"11111111", b"22222222", b"33333333", b"44444444"):
                    capture.write(value)
                self.assertEqual(path.read_bytes(), b"44444444")
                self.assertEqual(path.with_name("uart.bin.1").read_bytes(), b"33333333")
                self.assertEqual(path.with_name("uart.bin.2").read_bytes(), b"22222222")
                self.assertEqual(len(list(Path(temp).iterdir())), 3)
            finally:
                capture.close()


if __name__ == "__main__":
    unittest.main()
