# SPDX-License-Identifier: GPL-2.0
"""Offline native PTY tests for the guest acceptance client (no ARM/QEMU needed).

The fake erbctl only announces release. The test's PTY master provides a serial
peer, independent of child stdout/stdin, to exercise raw mode and cleanup.
"""
import errno
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BANNER = b"ERBIUM-UART-SMOKE-v1\r\n"
BINARY = b"\x00\r\n\x11\x13\xff"


class UartGuestClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.directory.name) / "uart-test"
        subprocess.run(
            ["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(cls.binary),
             str(ROOT / "linux/tools/erbium-uart-test.c")], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def exercise(self, mode="echo"):
        with tempfile.TemporaryDirectory() as d:
            event = Path(d) / "releases"
            event.write_text("")
            fake = Path(d) / "erbctl"
            fake.write_text("""#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
assert sys.argv[1:] == ["-d", "/dev/erbium0", "load", "/firmware/uart-smoke.elf",
                        "--mtd", "/dev/mtd0", "--verify", "--start"]
# A descriptor leaked to erbctl could steal the banner.
for p in Path("/proc/self/fd").iterdir():
    try:
        assert os.readlink(p) != os.environ["TEST_TTY"]
    except FileNotFoundError:
        pass
with open(os.environ["TEST_RELEASES"], "a") as out:
    out.write(json.dumps(sys.argv[1:]) + "\\n")
    out.flush()
if os.environ["TEST_MODE"] == "loader-fail":
    sys.exit(7)
if os.environ["TEST_MODE"] == "loader-hang":
    time.sleep(10)
""")
            fake.chmod(0o755)
            master, slave = pty.openpty()
            tty_name = os.ttyname(slave)
            original = termios.tcgetattr(slave)
            if mode == "pre-release-stale":
                original[3] &= ~termios.ECHO
                termios.tcsetattr(slave, termios.TCSANOW, original)
                os.write(master, b"\x00")
            os.set_blocking(master, False)
            env = dict(os.environ, TEST_TTY=tty_name, TEST_RELEASES=str(event),
                       TEST_MODE=mode)
            process = subprocess.Popen(
                [str(self.binary), "--tty", tty_name, "--erbctl", str(fake),
                 "--timeout-ms", "1000"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env)
            traffic = bytearray()
            releases = 0
            pending = bytearray()
            signal_sent = False
            exclusive_seen = False
            deadline = time.monotonic() + 6
            try:
                while process.poll() is None and time.monotonic() < deadline:
                    count = len(event.read_text().splitlines())
                    if count > releases:
                        self.assertEqual(count, releases + 1)
                        releases = count
                        # Same TTY must still be exclusive at each CPU restart.
                        try:
                            extra = os.open(tty_name, os.O_RDWR | os.O_NOCTTY)
                        except OSError as e:
                            self.assertEqual(e.errno, errno.EBUSY)
                            exclusive_seen = True
                        else:
                            os.close(extra)
                            # CAP_SYS_ADMIN may bypass TIOCEXCL; flock remains.
                            self.assertEqual(os.getuid(), 0)
                        if mode == "echo":
                            pending += BANNER
                        elif mode == "wrong-banner":
                            pending += b"!" + BANNER
                        elif mode == "duplicate-banner":
                            pending += BANNER + BANNER
                        elif mode == "wrong-echo":
                            pending += BANNER
                        elif mode == "signal" and not signal_sent:
                            os.kill(process.pid, signal.SIGTERM)
                            signal_sent = True
                    r, w, _ = select.select(
                        [master], [master] if pending else [], [], 0.005)
                    if r:
                        try:
                            data = os.read(master, 4096)
                        except BlockingIOError:
                            data = b""
                        traffic += data
                        if mode == "echo":
                            pending += data
                        elif mode == "wrong-echo" and data:
                            pending += bytes([data[0] ^ 1]) + data[1:]
                    if w:
                        # Fragment banners/echo to exercise partial reads.
                        n = os.write(master, pending[:7])
                        del pending[:n]
                stdout, stderr = process.communicate(timeout=2)
                self.assertEqual(termios.tcgetattr(slave), original,
                                 "client must restore all original termios")
                # Exclusion must be removed on success, failures and SIGTERM.
                fd = os.open(tty_name, os.O_RDWR | os.O_NOCTTY)
                os.close(fd)
                if os.getuid() != 0 and releases:
                    self.assertTrue(exclusive_seen)
                return process.returncode, stdout, stderr, releases, bytes(traffic)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(slave)
                os.close(master)

    def test_binary_burst_repeat_and_restore(self):
        rc, stdout, stderr, releases, traffic = self.exercise()
        self.assertEqual(rc, 0, stderr.decode())
        self.assertEqual(stdout, b"ALL UART TESTS PASSED\n")
        self.assertEqual(releases, 2)
        expected = b"".join(
            BINARY + bytes((i + round_number * 37) % 256 for i in range(1024))
            for round_number in range(2))
        self.assertEqual(traffic, expected)

    def test_wrong_banner_is_not_searched_past(self):
        rc, stdout, stderr, releases, _ = self.exercise("wrong-banner")
        self.assertNotEqual(rc, 0)
        self.assertEqual(stdout, b"")
        self.assertEqual(releases, 1)
        self.assertIn(b"banner offset 0", stderr)

    def test_duplicate_banner_is_stale_input(self):
        rc, _, stderr, releases, _ = self.exercise("duplicate-banner")
        self.assertNotEqual(rc, 0)
        self.assertEqual(releases, 1)
        self.assertIn(b"stale/extra byte", stderr)

    def test_stale_bytes_before_release_prevent_load(self):
        rc, _, stderr, releases, _ = self.exercise("pre-release-stale")
        self.assertNotEqual(rc, 0)
        self.assertEqual(releases, 0)
        self.assertIn(b"stale/extra byte 0x00", stderr)

    def test_wrong_echo_fails(self):
        rc, _, stderr, _, _ = self.exercise("wrong-echo")
        self.assertNotEqual(rc, 0)
        self.assertIn(b"echo mismatch", stderr)

    def test_missing_banner_has_wallclock_timeout(self):
        rc, _, stderr, _, _ = self.exercise("no-banner")
        self.assertNotEqual(rc, 0)
        self.assertIn(b"wall-clock timeout", stderr)

    def test_loader_failure(self):
        rc, _, stderr, _, _ = self.exercise("loader-fail")
        self.assertNotEqual(rc, 0)
        self.assertIn(b"erbctl load --verify --start failed", stderr)

    def test_loader_hang_has_wallclock_timeout(self):
        rc, _, stderr, _, _ = self.exercise("loader-hang")
        self.assertNotEqual(rc, 0)
        self.assertIn(b"wall-clock timeout", stderr)

    def test_signal_cleanup(self):
        rc, stdout, stderr, _, _ = self.exercise("signal")
        self.assertNotEqual(rc, 0)
        self.assertEqual(stdout, b"")
        self.assertIn(b"interrupted", stderr)

    def test_repeat_is_mandatory(self):
        r = subprocess.run([str(self.binary), "--rounds", "1"], capture_output=True)
        self.assertEqual(r.returncode, 2)

    def test_stdio_must_not_be_serial_peer(self):
        master, slave = pty.openpty()
        try:
            original = termios.tcgetattr(slave)
            r = subprocess.run([str(self.binary), "--tty", os.ttyname(slave)],
                               stdout=slave, stderr=subprocess.PIPE, timeout=2)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn(b"must not be stdin/stdout/stderr", r.stderr)
            self.assertEqual(termios.tcgetattr(slave), original)
        finally:
            os.close(slave)
            os.close(master)


if __name__ == "__main__":
    unittest.main()
