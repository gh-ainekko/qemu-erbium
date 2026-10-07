#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Helper unit tests. These are not browser/firmware interoperability evidence."""
import unittest

from web_ide_driver import assert_clean_browser_gp_log, browser_driver, replace_once, verify_wire


class DriverTests(unittest.TestCase):
    def test_driver_preserves_real_runtime(self):
        source = browser_driver()
        for stage in ("Prepare", "ObserveRunning", "ObserveStopped", "Roundtrip"):
            self.assertIn(f"to webIde{stage} {{", source)
        self.assertIn("compiledBytesFor rt", source)
        self.assertIn("stopAndSyncScripts rt", source)
        self.assertIn("ideReadback rt", source)
        self.assertIn("processNextMessage rt", source)
        self.assertIn("installDecompiledProject rt recovered", source)
        for forbidden in ("to startup", "method ", "writeFile", "readFile",
                          "newPage", "new 'MicroBlocksEditor'", "native save", " exit\n",
                          "fileTransferProgress", "launch ", "to ideConnect"):
            self.assertNotIn(forbidden, source)

    def test_adaptation_fails_on_native_driver_drift(self):
        for source in ("nothing", "xx"):
            with self.assertRaises(AssertionError):
                replace_once(source, "x", "new")

    def test_no_uart_is_not_success(self):
        with self.assertRaises(AssertionError):
            verify_wire(b"", b"", dict(chunk_count=30, incrementally_changed_chunks=1,
                                       recompiled_chunk_count=30))

    def test_real_browser_banner_is_not_native_repl(self):
        startup = ["log: Loaded 160 library files from embedded file system", "log: Welcome to GP!"]
        assert_clean_browser_gp_log(startup + ["log: IDE_TEST_PASS true"])
        for bad in ("gp> ", "Welcome to GP!", "Undefined function: foobar", "Serial error, length: 1024"):
            with self.subTest(bad=bad), self.assertRaises(AssertionError):
                assert_clean_browser_gp_log(startup + ["log: " + bad])


class WireTests(unittest.TestCase):
    @staticmethod
    def frame(opcode, ident=0, body=None):
        if body is None:
            return bytes([250, opcode, ident])
        return bytes([251, opcode, ident]) + len(body).to_bytes(2, "little") + body

    def fixture(self):
        # Synthetic framing *unit* data only, never interoperability evidence.
        chunks = {i: bytes([3]) + bytes([i]) * 900 for i in range(24)}
        initial = b"".join(self.frame(32, i, body + b"\xfe") for i, body in chunks.items())
        chunks[0] = bytes([3]) + bytes([42]) * 900
        outgoing = (self.frame(30, 1, b"\x01\xfe") + initial
                    + self.frame(30, 1, b"\x01\xfe")
                    + self.frame(32, 0, chunks[0] + b"\xfe") + self.frame(13, 1))
        outgoing += b"".join(self.frame(32, i, body + b"\xfe") for i, body in chunks.items())
        incoming = self.frame(21, 0, b"\x01\x29\x00\x00\x00")
        incoming += b"".join(self.frame(32, i, body) for i, body in chunks.items())
        result = dict(chunk_count=24, incrementally_changed_chunks=1, recompiled_chunk_count=24)
        artifact = [[ident, body[0], list(body[1:])] for ident, body in chunks.items()]
        return outgoing, incoming, result, artifact

    def test_complete_upload_readback(self):
        summary = verify_wire(*self.fixture())
        self.assertEqual(summary["downloaded_chunk_frames"], 49)
        self.assertTrue(summary["wire_readback_bytes_exact"])

    def test_redundant_upload_rejected(self):
        outgoing, incoming, result, artifact = self.fixture()
        outgoing += self.frame(32, 0, b"\x03\xfe")
        with self.assertRaisesRegex(AssertionError, "redundant"):
            verify_wire(outgoing, incoming, result, artifact)

    def test_readback_bytes_must_match_actual_upload(self):
        outgoing, incoming, result, artifact = self.fixture()
        incoming = incoming[:-1] + b"\xff"
        with self.assertRaisesRegex(AssertionError, "differs"):
            verify_wire(outgoing, incoming, result, artifact)

    def test_gp_artifact_not_trusted(self):
        outgoing, incoming, result, artifact = self.fixture()
        artifact[0][2][0] ^= 1
        with self.assertRaisesRegex(AssertionError, "artifact"):
            verify_wire(outgoing, incoming, result, artifact)

    def test_wrong_default_profile_rejected(self):
        outgoing, incoming, result, artifact = self.fixture()
        outgoing = outgoing.replace(b"\xfb\x1e\x01\x02\x00\x01\xfe",
                                    b"\xfb\x1e\x01\x02\x00\x02\xfe")
        with self.assertRaises(AssertionError):
            verify_wire(outgoing, incoming, result, artifact)

    def test_incomplete_frames_rejected(self):
        outgoing, incoming, result, artifact = self.fixture()
        with self.assertRaisesRegex(AssertionError, "incomplete"):
            verify_wire(outgoing, incoming[:-1], result, artifact)


if __name__ == "__main__":
    unittest.main()
